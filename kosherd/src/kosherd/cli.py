"""kosherctl — CLI for kosherd (admin/support/dev).

Online commands talk to the daemon over D-Bus (polkit will prompt).
Offline commands (validate / render-nft / render-dnsmasq) run the pure policy
engine locally — the fast dev loop for enforcement changes.
"""

from __future__ import annotations

import argparse
import getpass
import logging
import os
import json
import re
import sys
from pathlib import Path

from . import dns, nft, policy as policy_mod

log = logging.getLogger(__name__)


def _client():
    from .client import DaemonClient  # Gio import deferred: offline cmds need no GObject

    return DaemonClient()


def _guardian_pw(args) -> str:
    if getattr(args, "guardian_password", None):
        return args.guardian_password
    if _client().guardian_enabled():
        return getpass.getpass("Guardian password: ")
    return ""


def cmd_log(args) -> int:
    """The filter's recent journal, filtered by a pattern if given: what a
    person is asked for when a block does not happen, on a machine with
    no root to run journalctl."""
    import re

    text = _client().filter_log(args.lines)
    pattern = re.compile(args.grep, re.I) if args.grep else None
    shown = 0
    for line in text.splitlines():
        if pattern is None or pattern.search(line):
            print(line)
            shown += 1
    if pattern is not None and not shown:
        print(f"(nothing matching {args.grep!r} in the last {args.lines} lines)")
    return 0


def cmd_status(args) -> int:
    pol = _client().get_policy()
    print(f"policy revision {pol['revision']} (source: {pol['source']}, "
          f"guardian: {'on' if pol['guardian']['enabled'] else 'off'})")
    for u in pol["users"]:
        extra = f" whitelist={len(u.get('whitelist', []))} domains" if u["mode"] == "whitelist" else ""
        print(f"  {u['username']} (uid {u['uid']}): {u['mode']}{extra}")
    try:
        status = _client().filter_status()
    except Exception:  # noqa: BLE001 - status must not break the listing
        status = {}
    if status.get("pictures") == "too_slow":
        print(f"  ! pictures are hidden, not checked: "
              f"{status.get('detect_ms')} ms each on this computer",
              file=sys.stderr)
    elif status.get("pictures") == "no_model":
        print("  ! pictures are hidden, not checked: no model installed",
              file=sys.stderr)
    for service in status.get("degraded", []):
        print(f"  ! {service} should be running and is not", file=sys.stderr)
    for problem in status.get("problems", []):
        print(f"  ! {problem}", file=sys.stderr)

    guest = pol.get("guest", {"enabled": False})
    if guest["enabled"]:
        print(f"  guest: enabled, mode {guest.get('mode', 'whitelist')}")
    else:
        print("  guest: disabled")
    return 0


def cmd_lists(args) -> int:
    """What the filter is actually holding, as opposed to what it ships."""
    status = _client().filter_status()
    portal = status.get("lists_version", 0)
    print(f"portal list updates: {'v' + str(portal) if portal else 'none yet'}")
    for entry in status.get("lists", []):
        mark = " " if entry["ok"] else "!"
        source = entry["source"] or "not found"
        print(f" {mark} {entry['name']:<18} {entry['entries']:>9,} entries"
              f"   {source}")
    return 0


# What each editable list is called to a person, and what an entry means.
EDITABLE = {
    "words": ("wordlist.json", "bad language, cleaned up on the page"),
    "searches": ("search-blocklist.json", "searches that will not run"),
    "content": ("content-terms.json", "words a page is judged by"),
}


def cmd_words(args) -> int:
    """Add or remove a handful of entries from one of the shipped lists."""
    if args.list_name not in EDITABLE:
        print(f"choose one of: {', '.join(EDITABLE)}", file=sys.stderr)
        return 1
    name, what = EDITABLE[args.list_name]
    c = _client()
    edits = c.get_list_edits(name)

    if args.action == "show":
        print(f"{args.list_name}: {what}")
        print(f"  {edits['shipped']} entries ship with KosherOS")
        added = edits["add"]
        if added:
            print(f"  added here: {json.dumps(added)}")
        if edits["remove"]:
            print(f"  removed here: {', '.join(edits['remove'])}")
        if not added and not edits["remove"]:
            print("  no local changes")
        return 0

    add, remove = edits["add"], list(edits["remove"])
    if args.action == "add":
        if name == "wordlist.json":
            if not args.replacement:
                print("a word needs what to replace it with, e.g.\n"
                      "  kosherctl words add words blast --replacement bother",
                      file=sys.stderr)
                return 1
            add = {**add, args.term: args.replacement}
        elif name == "search-blocklist.json":
            add = sorted({*(add or []), args.term}) if isinstance(add, list) \
                else sorted({args.term})
        else:
            level = args.level or "immodest"
            weight = str(args.weight or 12)
            add = dict(add)
            add.setdefault(level, {}).setdefault(weight, [])
            if args.term not in add[level][weight]:
                add[level][weight].append(args.term)
        remove = [t for t in remove if t != args.term]
    elif args.action == "remove":
        if args.term not in remove:
            remove.append(args.term)
        if isinstance(add, dict) and name == "wordlist.json":
            add = {k: v for k, v in add.items() if k != args.term}
        elif isinstance(add, list):
            add = [t for t in add if t != args.term]

    c.edit_list(name, add, remove, _guardian_pw(args))
    print(f"{args.list_name}: saved")
    return 0


def cmd_get_policy(args) -> int:
    print(json.dumps(_client().get_policy(), indent=2))
    return 0


def cmd_set_mode(args) -> int:
    _client().set_filter_mode(args.uid, args.mode, _guardian_pw(args))
    print(f"uid {args.uid} -> {args.mode}")
    return 0


def cmd_set_whitelist(args) -> int:
    _client().set_whitelist(args.uid, args.domains, _guardian_pw(args))
    print(f"uid {args.uid}: {len(args.domains)} domains")
    return 0


def cmd_create_user(args) -> int:
    uid = _client().create_user(args.username, args.full_name or args.username, args.mode)
    print(f"created {args.username} (uid {uid}, mode {args.mode})")
    return 0


def cmd_guest(args) -> int:
    enabled = args.action == "enable"
    _client().set_guest_config(enabled, args.mode, args.domains, _guardian_pw(args))
    print(f"guest account {'enabled' if enabled else 'disabled'}"
          + (f" (mode {args.mode})" if enabled else ""))
    return 0


def cmd_adopt(args) -> int:
    uid = _client().adopt_user(args.username, args.mode)
    print(f"adopted {args.username} (uid {uid}, mode {args.mode})")
    return 0


def cmd_rules(args) -> int:
    c = _client()
    if args.action == "list":
        user = next((u for u in c.get_policy()["users"] if u["uid"] == args.uid), None)
        if user is None:
            print(f"uid {args.uid} is not managed", file=sys.stderr)
            return 1
        rules = user.get("rules", [])
        print(f"{len(rules)} rule(s) for {user['username']} (mode: {user['mode']})")
        for i, r in enumerate(rules, 1):
            print(f"  {i}. {r['action']:5} {r['pattern']}")
        if rules and user["mode"] != "filtered":
            print("  note: rules only apply in filtered mode", file=sys.stderr)
        return 0

    user = next((u for u in c.get_policy()["users"] if u["uid"] == args.uid), None)
    rules = list(user.get("rules", [])) if user else []
    if args.action in ("allow", "block"):
        rules.append({"action": args.action, "pattern": args.pattern})
    elif args.action == "remove":
        index = int(args.pattern) - 1
        if not 0 <= index < len(rules):
            print(f"no rule {args.pattern}", file=sys.stderr)
            return 1
        rules.pop(index)
    elif args.action == "clear":
        rules = []
    c.set_url_rules(args.uid, rules, _guardian_pw(args))
    print(f"{len(rules)} rule(s) now set for uid {args.uid}")
    return 0


def cmd_admin(args) -> int:
    """Make an account an administrator, or stop it being one."""
    _client().set_user_admin(args.uid, args.action == "grant",
                             _guardian_pw(args))
    print(f"uid {args.uid} is "
          + ("now an administrator" if args.action == "grant"
             else "no longer an administrator"))
    return 0


def cmd_profile(args) -> int:
    c = _client()
    if args.action == "list":
        listed = c.list_profiles()
        if not listed:
            print("  no groups yet: `kosherctl group save <uid> <name>` makes one "
                  "from an account's settings")
        for p in listed:
            print(f"  {p['key']:<24} {p['label']}")
            if p.get("description"):
                print(f"                           {p['description']}")
        return 0
    if args.action == "delete":
        key = args.profile or args.uid  # `group delete custom-x`
        c.delete_profile(key, _guardian_pw(args))
        print(f"deleted group {key}; its members keep their settings")
        return 0
    try:
        uid = int(args.uid)
    except ValueError:
        print(f"error: {args.uid!r} is not a uid", file=sys.stderr)
        return 2
    if args.action == "save":
        key = c.save_profile(uid, args.profile, "", _guardian_pw(args))
        print(f"saved uid {uid}'s settings as group {key}; every account in it follows")
        return 0
    key = "" if args.profile in ("", "none") else args.profile
    c.apply_profile(uid, key, _guardian_pw(args))
    print(f"uid {uid} put in group {key}" if key else f"uid {uid} taken out of its group")
    return 0


def cmd_site_lists(args) -> int:
    """The ready-made approved-site lists, and which an account uses."""
    c = _client()
    bundles = c.list_whitelist_bundles()
    if args.action == "show" and not args.username:
        for bundle in bundles:
            print(f"  {bundle['key']:<16} {bundle['label']}  ({bundle['domains']} sites)")
            print(f"    {bundle['description']}")
        return 0
    policy = c.get_policy()
    user = next((u for u in policy["users"] if u["username"] == args.username), None)
    if user is None:
        print(f"no managed account called {args.username!r}", file=sys.stderr)
        return 1
    if args.action == "show":
        on = set(user.get("whitelist_bundles") or [])
        for bundle in bundles:
            print(f"  [{'x' if bundle['key'] in on else ' '}] {bundle['key']:<16} "
                  f"{bundle['label']}")
        return 0
    c.set_whitelist_bundles(user["uid"], args.bundles, _guardian_pw(args))
    print(f"{args.username}: {', '.join(args.bundles) or 'no ready-made lists'}")
    return 0


def cmd_activity(args) -> int:
    """The filter's diary: blocks, hidden pictures, refused searches, and
    settings changes, each with who and why."""
    import time

    from . import activity as activity_mod

    c = _client()
    if args.days <= 1:
        since = activity_mod.day_start()
    else:
        since = int(time.time()) - args.days * 86400
    uid = -1
    if args.user:
        uid = next((u["uid"] for u in c.get_policy()["users"]
                    if u["username"] == args.user), None)
        if uid is None:
            print(f"no managed account called {args.user!r}", file=sys.stderr)
            return 1
    found = c.list_activity(since, uid)
    if not found:
        print("nothing recorded")
        return 0
    for e in found:
        when = time.strftime("%a %H:%M", time.localtime(e["t"]))
        who = e.get("username", "?")
        kind = e["kind"]
        if kind == "change":
            what = f"{e.get('by_username', '?')} changed {who}: {e.get('method')}"
            if e.get("guardian"):
                what += "  (guardian)"
        elif kind == "block":
            what = f"{who:<12} blocked   {e.get('url', '')}  [{e.get('why', '')}]"
        elif kind == "pictures":
            what = f"{who:<12} pictures  {e.get('url', '')}"
        elif kind == "video":
            what = f"{who:<12} video     {e.get('url', '')}"
        else:
            what = f"{who:<12} search    {e.get('text', '')!r}  [{e.get('why', '')}]"
        print(f"  {when}  {what}")
    return 0


def cmd_requests(args) -> int:
    c = _client()
    waiting = c.list_requests()
    if args.action == "list":
        if not waiting:
            print("nothing waiting")
            return 0
        for r in waiting:
            note = f"  ({r['note']})" if r.get("note") else ""
            print(f"  {r['id'][:8]}  {r['username']:<12} {r['url']}{note}")
        return 0

    match = [r for r in waiting if r["id"].startswith(args.request_id)]
    if len(match) != 1:
        print(f"{len(match)} requests match {args.request_id!r}", file=sys.stderr)
        return 1
    if args.action == "allow":
        c.approve_request(match[0]["id"], args.whole_site, _guardian_pw(args))
        print(f"allowed for {match[0]['username']}")
    else:
        c.dismiss_request(match[0]["id"])
        print("dismissed")
    return 0


def cmd_media(args) -> int:
    c = _client()
    c.set_media_level(args.uid, args.level, _guardian_pw(args))
    print(f"uid {args.uid}: pictures -> {args.level}")
    return 0


def cmd_apps(args) -> int:
    """What the Store offers an account, and what is blocked from it."""
    from kosherd import appaccess, appkinds

    c = _client()
    if args.action == "kinds":
        for key in appkinds.KIND_KEYS:
            print(f"  {key:<10} {appkinds.label(key)}")
        return 0

    user = next((u for u in c.get_policy()["users"] if u["uid"] == args.uid), None)
    if user is None:
        print(f"uid {args.uid} is not managed", file=sys.stderr)
        return 1
    access_key = user.get("app_access") or ("store" if user.get("admin") else "approved")
    kinds = list(user.get("blocked_app_kinds", []))
    blocked = list(user.get("blocked_apps", []))

    if args.action == "show":
        print(f"{user['username']}: {appaccess.APP_ACCESS_LABELS[access_key]}")
        print("  blocked kinds: " + (", ".join(kinds) if kinds else "none"))
        print("  blocked apps:  " + (", ".join(blocked) if blocked else "none"))
        print("  can install:   " + ("yes" if user.get("can_install_apps", True) else "no"))
        return 0
    if args.action == "access":
        if args.values[:1] not in (["approved"], ["store"]):
            print("usage: kosherctl apps access <uid> approved|store", file=sys.stderr)
            return 2
        c.set_user_app_access(args.uid, args.values[0], _guardian_pw(args))
        print(f"{user['username']}: {appaccess.APP_ACCESS_LABELS[args.values[0]]}")
        return 0
    if args.action in ("block-kind", "unblock-kind"):
        bad = [k for k in args.values if not appkinds.is_kind(k)]
        if bad:
            print(f"unknown kind: {', '.join(bad)} (see 'kosherctl apps kinds')",
                  file=sys.stderr)
            return 2
        kinds = (sorted(set(kinds) | set(args.values)) if args.action == "block-kind"
                 else [k for k in kinds if k not in args.values])
        c.set_user_blocked_app_kinds(args.uid, kinds, _guardian_pw(args))
        print(f"{user['username']} blocks kinds: " + (", ".join(kinds) if kinds else "none"))
        return 0
    if args.action in ("block", "unblock"):
        blocked = (sorted(set(blocked) | set(args.values)) if args.action == "block"
                   else [r for r in blocked if r not in args.values])
        c.set_user_blocked_apps(args.uid, blocked, _guardian_pw(args))
        print(f"{user['username']} blocks apps: " + (", ".join(blocked) if blocked else "none"))
        return 0
    return 2


def cmd_layout(args) -> int:
    from .policy import LAYOUT_LABELS

    c = _client()
    c.set_layout(args.uid, args.layout)
    print(f"uid {args.uid}: desktop -> {LAYOUT_LABELS[args.layout]} "
          f"(takes effect at their next sign-in)")
    return 0


def cmd_cover(args) -> int:
    from .policy import COVER_STYLE_LABELS

    c = _client()
    c.set_cover_style(args.uid, args.style)
    print(f"uid {args.uid}: covered pictures -> {COVER_STYLE_LABELS[args.style].lower()}")
    return 0


def cmd_adblock(args) -> int:
    c = _client()
    on = args.state == "on"
    c.set_adblock(on, _guardian_pw(args) if not on else "")
    print(f"ad and tracker blocking {'on' if on else 'off'} for every account")
    return 0


def cmd_language(args) -> int:
    c = _client()
    c.set_language_filter(args.uid, args.setting, _guardian_pw(args))
    print(f"uid {args.uid}: bad language -> {args.setting}")
    return 0


def cmd_youtube(args) -> int:
    from .policy import YOUTUBE_CATEGORIES

    c = _client()
    if args.action == "categories":
        for code, label in sorted(YOUTUBE_CATEGORIES.items(),
                                  key=lambda kv: kv[1]):
            print(f"  {code:>3}  {label}")
        return 0

    user = next((u for u in c.get_policy()["users"] if u["uid"] == args.uid), None)
    if user is None:
        print(f"uid {args.uid} is not managed", file=sys.stderr)
        return 1
    settings = dict(user.get("youtube") or {})
    blocked = set(settings.get("blocked_categories", []))
    channels = list(settings.get("allowed_channels", []))

    if args.action == "show":
        print(f"{user['username']}: Restricted Mode "
              f"{settings.get('restrict', 'moderate')}")
        print("  approved channels: "
              + (", ".join(channels) if channels else "every channel"))
        print("  blocked kinds: "
              + (", ".join(f"{c} ({YOUTUBE_CATEGORIES.get(c, '?')})"
                           for c in sorted(blocked)) if blocked else "none"))
        if user["mode"] != "filtered":
            print("  note: YouTube limits only apply in filtered mode",
                  file=sys.stderr)
        return 0
    if args.action == "restrict":
        settings["restrict"] = args.value
    elif args.action == "block":
        blocked |= set(args.value.split(","))
        settings["blocked_categories"] = sorted(blocked)
    elif args.action == "unblock":
        blocked -= set(args.value.split(","))
        settings["blocked_categories"] = sorted(blocked)
    elif args.action == "allow-channel":
        if args.value not in channels:
            channels.append(args.value)
        settings["allowed_channels"] = channels
    elif args.action == "remove-channel":
        settings["allowed_channels"] = [c for c in channels if c != args.value]
    c.set_youtube(args.uid, settings, _guardian_pw(args))
    print(f"YouTube settings saved for {user['username']}")
    return 0


def cmd_categories(args) -> int:
    c = _client()
    if args.action == "available":
        info = c.list_categories()
        print(f"{info['domains']} domains, list version {info['version']}"
              + (f" ({info['source']})" if info["source"] else ""))
        for cat in info["categories"]:
            print(f"  {cat['name']:<10} {cat['label']}")
        return 0

    user = next((u for u in c.get_policy()["users"] if u["uid"] == args.uid), None)
    if user is None:
        print(f"uid {args.uid} is not managed", file=sys.stderr)
        return 1
    blocked = list(user.get("blocked_categories", []))

    if args.action == "list":
        print(f"{user['username']} ({user['mode']}) blocks: "
              + (", ".join(blocked) if blocked else "nothing"))
        if blocked and user["mode"] == "unfiltered":
            print("  note: an unfiltered account enforces nothing", file=sys.stderr)
        return 0
    if args.action == "block":
        blocked = sorted(set(blocked) | set(args.categories))
    elif args.action == "allow":
        blocked = [c for c in blocked if c not in args.categories]
    elif args.action == "clear":
        blocked = []
    c.set_blocked_categories(args.uid, blocked, _guardian_pw(args))
    print(f"{user['username']} now blocks: "
          + (", ".join(blocked) if blocked else "nothing"))
    return 0


def cmd_portal(args) -> int:
    c = _client()
    if args.action == "status":
        st = c.portal_status()
        if not st["enrolled"]:
            print("not enrolled with a portal "
                  f"(local policy revision {st['revision']})")
            return 0
        print(f"enrolled with {st['portal_url']} as {st['device_id']}")
        print(f"  policy revision {st['revision']} (source: {st['source']})")
        return 0
    if args.action == "enrol":
        if not args.value:
            print("usage: kosherctl portal enrol <url> --code <code>", file=sys.stderr)
            return 1
        device_id = c.enrol(args.value, args.code or "", _guardian_pw(args))
        print(f"enrolled as {device_id}")
        return 0
    if args.action == "unenrol":
        c.unenrol(_guardian_pw(args))
        print("unenrolled; the policy on this device stays as it is")
        return 0
    return 1


def _deployment_line(label: str, entry) -> str:
    if not entry:
        return f"  {label:<10} none"
    version = entry.get("version") or "unknown version"
    image = entry.get("image") or "unknown image"
    return f"  {label:<10} {version}\n             {image}"


def cmd_system(args) -> int:
    c = _client()
    if args.action == "status":
        st = c.deployment_status()
        if st.get("error"):
            print(f"could not read the system version: {st['error']}",
                  file=sys.stderr)
            return 1
        print("system version:")
        print(_deployment_line("booted", st.get("booted")))
        if st.get("staged"):
            print(_deployment_line("staged", st["staged"]))
            print("             takes effect at the next restart")
        print(_deployment_line("go back to", st.get("rollback")))
        if st.get("rollback_queued"):
            print("  a rollback is already queued for the next restart")
        return 0
    if args.action == "check":
        print(check_sentence(c.check_update()))
        return 0
    if args.action == "update":
        c.apply_update()
        print("update staged; it takes effect at the next restart")
        return 0
    if args.action == "rollback":
        st = c.deployment_status()
        target = st.get("rollback")
        if not target:
            print("there is no previous version to go back to",
                  file=sys.stderr)
            return 1
        c.rollback()
        print(f"going back to {target.get('version') or 'the previous version'}"
              " at the next restart")
        return 0
    if args.action == "channel":
        info = c.list_channels()
        if not args.value:
            print(f"this computer follows: {info.get('current') or 'no channel'}"
                  f" ({info.get('image') or 'unknown image'})")
            if info.get("pending"):
                # bootc leaves the running deployment alone, so without this
                # the listing would say a switch already made had not been.
                print(f"  moving to {info['pending']} at the next restart")
            print("channels:")
            for entry in info.get("channels", []):
                mark = "*" if entry.get("current") else " "
                print(f"  {mark} {entry['name']:<7} {entry['summary']}")
            if not (info.get("current") or info.get("pending")):
                print("\nthis computer is fixed at one version and will not "
                      "update on its own; name a channel to start getting "
                      "updates again")
            if (info.get("pending") or info.get("current")) != "stable":
                print("\nstable is the one to be on unless you are helping "
                      "test KosherOS")
            return 0
        wanted = args.value.strip().lower()
        known = [e["name"] for e in info.get("channels", [])]
        if wanted not in known:
            print(f"unknown channel: {args.value} (try {' or '.join(known)})",
                  file=sys.stderr)
            return 1
        c.set_channel(wanted, _guardian_pw(args))
        print(f"switching to {wanted}; the download runs in the background "
              "and the new version is used from the next restart\n"
              "run 'kosherctl system status' to see it staged")
        return 0
    return 1


def cmd_sync(args) -> int:
    try:
        updated = _client().sync_now()
    except Exception:  # noqa: BLE001 - the timer must not spam failures
        if args.quiet:
            return 0
        raise
    if not args.quiet:
        print("applied a new policy from the portal" if updated
              else "already up to date")
    return 0


def _isatty(stream) -> bool:
    """Whether a stream is a real terminal, tolerant of stand-ins.

    A test or a pipe hands us something with no fileno; that is simply not
    a tty rather than an error.
    """
    try:
        return os.isatty(stream.fileno())
    except (OSError, ValueError, AttributeError):
        return False


class _Console:
    """Talk to the person running setup over stdin/stdout.

    History, because this took three boots to get right. The wizard runs
    as a systemd service with StandardInput=tty, so stdin IS the console;
    reading from it always worked. What failed:

      1. a getty on the same console stole the input — fixed by Conflicts=;
      2. input()'s prompt has no trailing newline, and stdout here is a
         pipe to tee, so the prompt sat unflushed in the buffer and the
         person saw the banner then silence;
      3. reaching for /dev/tty to dodge (2) made it worse: a service has no
         controlling terminal, so /dev/tty does not exist for it.

    The fix for (2) is just to flush. No /dev/tty, no ttyname games: write
    the prompt to stdout and flush it, read the answer from stdin. The
    prompt reaches the journal and the console alike, which is why the boot
    test sees "Username:" whatever prefix journald adds.
    """

    def __init__(self):
        # Printed once, so a boot that still cannot prompt says why in the
        # log instead of hanging in silence. Cheap insurance against a
        # fourth round trip.
        log.info("setup console: stdin isatty=%s stdout isatty=%s",
                 _isatty(sys.stdin), _isatty(sys.stdout))

    def say(self, text: str = "") -> None:
        print(text, flush=True)

    def ask(self, prompt: str) -> str:
        # The flush is the whole point: without it the prompt waits in the
        # buffer while readline blocks, and nothing appears.
        sys.stdout.write(prompt)
        sys.stdout.flush()
        line = sys.stdin.readline()
        if not line:
            raise EOFError("the console closed mid-setup")
        return line.strip("\r\n")

    def ask_secret(self, prompt: str) -> str:
        """A password: read from stdin with echo off when stdin is a tty.

        getpass would do this, but it insists on /dev/tty and falls back to
        stdin with a warning and full echo when there is no controlling
        terminal — a password in the clear on the family setup console.
        Turning echo off on stdin itself avoids both.
        """
        try:
            fd = sys.stdin.fileno()
            is_tty = os.isatty(fd)
        except (OSError, ValueError):
            is_tty = False
        if not is_tty:
            return getpass.getpass(prompt)
        import termios

        sys.stdout.write(prompt)
        sys.stdout.flush()
        saved = termios.tcgetattr(fd)
        try:
            quiet = termios.tcgetattr(fd)
            quiet[3] &= ~termios.ECHO
            termios.tcsetattr(fd, termios.TCSADRAIN, quiet)
            line = sys.stdin.readline()
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, saved)
            sys.stdout.write("\n")  # the Enter the terminal did not echo
            sys.stdout.flush()
        if not line:
            raise EOFError("the console closed mid-setup")
        return line.strip("\r\n")

def cmd_setup(args) -> int:
    """Text-mode first-boot setup, for when the graphical wizard cannot run.

    Without this a machine whose GUI wizard fails has no account, no root
    password and no way in — an unrecoverable brick. A console prompt is
    ugly but it always works.
    """
    import re

    c = _client()
    if c.setup_complete():
        print("This computer is already set up.", file=sys.stderr)
        return 1

    # Non-interactive: scripted provisioning, remote support, and the
    # automated boot test all need setup without a console prompt.
    if args.username:
        if not args.password_stdin:
            print("--username requires --password-stdin", file=sys.stderr)
            return 1
        password = sys.stdin.readline().rstrip("\n")
        uid = c.create_first_admin(args.username, args.full_name or args.username,
                                   password)
        c.finish_setup(args.guardian_password or "", "")
        print(f"created {args.username} (uid {uid}); setup complete")
        return 0

    console = _Console()
    console.say("\nKosherOS setup\n")

    # Resume: an earlier run may have created the administrator and then
    # failed at a later step. Do not demand a second one.
    try:
        exists, who = c.admin_exists()
    except Exception:  # noqa: BLE001 - an older daemon; behave as before
        exists, who = False, ""
    if exists:
        console.say(f"The administrator account '{who}' already exists; "
                    "continuing with the remaining steps.\n")
    else:
        console.say("Create the administrator account for this computer.")
        console.say("It manages profiles, filtering and apps. It has no root access.\n")
        try:
            existing = c.existing_accounts()
        except Exception:  # noqa: BLE001 - an older daemon
            existing = []
        if existing:
            console.say(f"This computer already has an account: {', '.join(existing)}.")
            console.say("Enter its name to make it the administrator (the password you")
            console.say("set becomes its password), or a new name for a fresh account.\n")

        while True:
            prompt = f"Username [{existing[0]}]: " if existing else "Username: "
            username = console.ask(prompt).strip() or (existing[0] if existing else "")
            if re.fullmatch(r"[a-z_][a-z0-9_-]*", username or ""):
                break
            console.say("  Use lowercase letters, digits, - or _ "
                        "(starting with a letter).")

        full_name = console.ask(f"Full name [{username}]: ").strip() or username

        while True:
            password = console.ask_secret("Password: ")
            if len(password) < 6:
                console.say("  At least 6 characters, please.")
                continue
            if password != console.ask_secret("Confirm password: "):
                console.say("  Those did not match.")
                continue
            break

        uid = c.create_first_admin(username, full_name, password)
        console.say(f"\nCreated {username} (uid {uid}).")

    guardian = ""
    if console.ask("\nSet a guardian password (a second password required to "
                   "change filter settings)? [y/N]: ").strip().lower() \
            .startswith("y"):
        while True:
            guardian = console.ask_secret("Guardian password: ")
            if len(guardian) >= 6 and \
                    guardian == console.ask_secret("Confirm: "):
                break
            console.say("  At least 6 characters, and both must match.")

    c.finish_setup(guardian, "")
    console.say("\nSetup complete. Starting the login screen.\n")
    return 0


def check_sentence(info: dict) -> str:
    """One line from a CheckUpdate answer."""
    if info.get("available"):
        where = f" on the {info['channel']} channel" if info.get("channel") else ""
        return (f"update available: {info['version']}{where}" if info.get("version")
                else f"update available{where}")
    if info.get("available") is False:
        return "up to date"
    return (info.get("raw") or "").strip() or "no update information"


def cmd_check_catalog(args) -> int:
    """Verify every approved app actually exists on the remote."""
    from kosherd import apps

    available = apps.available_refs()
    catalog = apps.load_catalog().get("apps", [])
    missing = [a["ref"] for a in catalog if a["ref"] not in available]
    print(f"{len(catalog)} approved apps, {len(missing)} unavailable")
    for ref in missing:
        print(f"  MISSING: {ref}", file=sys.stderr)
    return 1 if missing else 0


def cmd_captive(args) -> int:
    _client().set_captive_mode(args.uid, args.minutes)
    print(f"captive-portal window open for uid {args.uid} ({args.minutes}m)")
    return 0


def cmd_guardian(args) -> int:
    c = _client()
    if args.action == "enable" or args.action == "set-password":
        old = getpass.getpass("Current guardian password (empty if none): ")
        new = getpass.getpass("New guardian password: ")
        if new != getpass.getpass("Repeat new guardian password: "):
            print("passwords do not match", file=sys.stderr)
            return 1
        c.set_guardian_password(old, new)
        print("guardian password set; guardian mode enabled")
    elif args.action == "disable":
        c.disable_guardian(getpass.getpass("Guardian password: "))
        print("guardian mode disabled")
    return 0


def cmd_update(args) -> int:
    c = _client()
    if args.apply:
        c.apply_update()
        print("update staged; reboot to apply")
    else:
        print(check_sentence(c.check_update()))
    return 0


def _load_local_policy(path: str) -> policy_mod.Policy:
    return policy_mod.Policy.from_dict(json.loads(Path(path).read_text()))


def cmd_validate(args) -> int:
    _load_local_policy(args.policy)
    print(f"{args.policy}: valid")
    return 0


def cmd_render_nft(args) -> int:
    """Render the ruleset exactly as the daemon would apply it.

    It used to pass only dns_uid, so it silently left out the redirect
    into the filtering proxy, the guard on the search back end and the
    plain resolver for unfiltered accounts — a different ruleset from the
    one actually enforced, printed by the command documented as the fast
    loop for enforcement changes.
    """
    from . import apply as apply_mod

    def uid_of(name: str) -> int | None:
        import pwd

        try:
            return pwd.getpwnam(name).pw_uid
        except KeyError:
            return None

    mitm_uid = uid_of("kosher-mitm")
    search_uid = uid_of("kosher-search")
    for name, found in (("kosher-mitm", mitm_uid), ("kosher-search", search_uid)):
        if found is None:
            # Say so rather than quietly omitting the rules that depend on
            # it: a missing line is the hardest kind of difference to see.
            print(f"# NOTE: no {name} user on this machine, so the rules "
                  f"that need it are missing from what follows.")
    try:
        dns_uid = args.dns_uid if args.dns_uid else apply_mod.dnsmasq_uid()
    except apply_mod.ApplyError:
        dns_uid = 0
        print("# NOTE: no dnsmasq user on this machine; using 0.")
    print(nft.render(_load_local_policy(args.policy), dns_uid=dns_uid,
                     mitm_uid=mitm_uid, search_uid=search_uid), end="")
    return 0


def cmd_render_dnsmasq(args) -> int:
    print(dns.render(_load_local_policy(args.policy)), end="")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="kosherctl", description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="show managed users and their filter modes").set_defaults(func=cmd_status)
    s = sub.add_parser("log", help="the filter's recent journal lines, no root needed")
    s.add_argument("-n", "--lines", type=int, default=200, help="how many lines (default 200)")
    s.add_argument("-g", "--grep", default="", help="show only lines matching this pattern")
    s.set_defaults(func=cmd_log)
    sub.add_parser("get-policy", help="dump the full policy JSON").set_defaults(func=cmd_get_policy)

    s = sub.add_parser("set-mode", help="change a user's filter mode")
    s.add_argument("uid", type=int)
    s.add_argument("mode", choices=policy_mod.MODES)
    s.add_argument("--guardian-password")
    s.set_defaults(func=cmd_set_mode)

    s = sub.add_parser("set-whitelist", help="replace a user's whitelist")
    s.add_argument("uid", type=int)
    s.add_argument("domains", nargs="*")
    s.add_argument("--guardian-password")
    s.set_defaults(func=cmd_set_whitelist)

    s = sub.add_parser("create-user", help="create a managed user account")
    s.add_argument("username")
    s.add_argument("mode", choices=policy_mod.MODES)
    s.add_argument("--full-name")
    s.set_defaults(func=cmd_create_user)

    s = sub.add_parser("guest", help="enable/disable and configure the guest account")
    s.add_argument("action", choices=["enable", "disable"])
    s.add_argument("--mode", choices=policy_mod.MODES, default="whitelist")
    s.add_argument("domains", nargs="*", help="guest whitelist (whitelist mode)")
    s.add_argument("--guardian-password")
    s.set_defaults(func=cmd_guest)

    s = sub.add_parser("adopt", help="bring an existing user under filter management")
    s.add_argument("username")
    s.add_argument("mode", choices=policy_mod.MODES)
    s.set_defaults(func=cmd_adopt)

    s = sub.add_parser("rules", help="URL allow/block rules (inspect mode)")
    s.add_argument("uid", type=int)
    s.add_argument("action", choices=["list", "allow", "block", "remove", "clear"])
    s.add_argument("pattern", nargs="?", default="",
                   help="URL pattern, or the rule number for 'remove'")
    s.add_argument("--guardian-password")
    s.set_defaults(func=cmd_rules)

    s = sub.add_parser("admin", help="grant or revoke administrator rights")
    s.add_argument("action", choices=["grant", "revoke"])
    s.add_argument("uid", type=int)
    s.add_argument("--guardian-password")
    s.set_defaults(func=cmd_admin)

    s = sub.add_parser("group", aliases=["profile"],
                       help="put an account in a group, save its settings as one, "
                            "or delete a group")
    s.add_argument("action", choices=["list", "set", "save", "delete"])
    # `delete` takes only a group key, so the first positional is not
    # always a uid; it is checked as one only where a uid is meant.
    s.add_argument("uid", nargs="?", default="0",
                   help="account uid (set/save); the group key for delete")
    s.add_argument("profile", nargs="?", default="",
                   help="group key (set) or name (save); `none` takes the account out")
    s.add_argument("--guardian-password")
    s.set_defaults(func=cmd_profile)

    s = sub.add_parser("words", help="add or remove a few entries from a list")
    s.add_argument("action", choices=["show", "add", "remove"])
    s.add_argument("list_name", choices=list(EDITABLE))
    s.add_argument("term", nargs="?", default="")
    s.add_argument("--replacement", help="what to substitute (words list)")
    s.add_argument("--level", choices=["immodest", "suggestive", "nsfw"],
                   help="how bad it is (content list)")
    s.add_argument("--weight", type=int, help="how much evidence it is worth")
    s.add_argument("--guardian-password")
    s.set_defaults(func=cmd_words)

    s = sub.add_parser("lists", help="what the filter is actually holding")
    s.set_defaults(func=cmd_lists)

    s = sub.add_parser("site-lists", help="ready-made approved-site lists")
    s.add_argument("action", choices=["show", "set"])
    s.add_argument("username", nargs="?", default="")
    s.add_argument("bundles", nargs="*", help="keys to switch on (none switches all off)")
    s.add_argument("--guardian-password")
    s.set_defaults(func=cmd_site_lists)

    s = sub.add_parser("activity", help="what the filter did, newest first")
    s.add_argument("--user", help="only this account")
    s.add_argument("--days", type=int, default=1, help="how far back (default: today)")
    s.set_defaults(func=cmd_activity)

    s = sub.add_parser("requests", help="pages users have asked for")
    s.add_argument("action", choices=["list", "allow", "dismiss"])
    s.add_argument("request_id", nargs="?", default="")
    s.add_argument("--whole-site", action="store_true",
                   help="allow the whole site, not just the page")
    s.add_argument("--guardian-password")
    s.set_defaults(func=cmd_requests)

    s = sub.add_parser("media", help="how much of the web's imagery to hide")
    s.add_argument("uid", type=int)
    s.add_argument("level", choices=list(policy_mod.MEDIA_LEVELS))
    s.add_argument("--guardian-password")
    s.set_defaults(func=cmd_media)

    s = sub.add_parser("language", help="what to do about bad language")
    s.add_argument("uid", type=int)
    s.add_argument("setting", choices=["off", "substitute", "block"])
    s.add_argument("--guardian-password")
    s.set_defaults(func=cmd_language)

    s = sub.add_parser("adblock", help="block ads and trackers for every account (like a Pi-hole)")
    s.add_argument("state", choices=["on", "off"])
    s.add_argument("--guardian-password")
    s.set_defaults(func=cmd_adblock)

    s = sub.add_parser("cover", help="how a user's covered pictures look")
    s.add_argument("uid", type=int)
    s.add_argument("style", choices=list(policy_mod.COVER_STYLES))
    s.set_defaults(func=cmd_cover)

    s = sub.add_parser("layout", help="how a user's desktop is laid out")
    s.add_argument("uid", type=int)
    s.add_argument("layout", choices=list(policy_mod.LAYOUTS))
    s.set_defaults(func=cmd_layout)

    s = sub.add_parser("youtube", help="YouTube limits for one user")
    s.add_argument("action", choices=["show", "categories", "restrict", "block",
                                      "unblock", "allow-channel",
                                      "remove-channel"])
    s.add_argument("uid", type=int, nargs="?", default=0)
    s.add_argument("value", nargs="?", default="")
    s.add_argument("--guardian-password")
    s.set_defaults(func=cmd_youtube)

    s = sub.add_parser("apps", help="what the Store offers a user, and what is blocked")
    s.add_argument("action", choices=["show", "access", "block-kind", "unblock-kind",
                                      "block", "unblock", "kinds"])
    s.add_argument("uid", type=int, nargs="?", default=0)
    s.add_argument("values", nargs="*", default=[],
                   help="approved|store, kinds (see 'kinds'), or app ids")
    s.add_argument("--guardian-password")
    s.set_defaults(func=cmd_apps)

    s = sub.add_parser("categories", help="content categories to block for a user")
    s.add_argument("uid", type=int, nargs="?", default=0)
    s.add_argument("action", choices=["list", "block", "allow", "clear", "available"])
    s.add_argument("categories", nargs="*", default=[])
    s.add_argument("--guardian-password")
    s.set_defaults(func=cmd_categories)

    s = sub.add_parser("portal", help="enrol with, or check, a remote portal")
    s.add_argument("action", choices=["status", "enrol", "unenrol"])
    s.add_argument("value", nargs="?", default="", help="portal URL when enrolling")
    s.add_argument("--code", help="one-time enrolment code")
    s.add_argument("--guardian-password")
    s.set_defaults(func=cmd_portal)

    s = sub.add_parser("system",
                       help="which system version is running, which channel "
                            "it follows, and going back")
    s.add_argument("action",
                   choices=["status", "check", "update", "rollback", "channel"])
    s.add_argument("value", nargs="?", default="",
                   help="with 'channel': the channel to switch to "
                        "(omit to list them)")
    s.add_argument("--guardian-password")
    s.set_defaults(func=cmd_system)

    s = sub.add_parser("sync", help="pull policy from the portal now")
    s.add_argument("--quiet", action="store_true",
                   help="stay silent and succeed even when the portal is unreachable")
    s.set_defaults(func=cmd_sync)

    s = sub.add_parser(
        "setup", help="text-mode first-boot setup (fallback for the GUI wizard)")
    s.add_argument("--username", help="run non-interactively for this admin")
    s.add_argument("--full-name")
    s.add_argument("--password-stdin", action="store_true",
                   help="read the password from stdin (with --username)")
    s.add_argument("--guardian-password", default="")
    s.set_defaults(func=cmd_setup)

    sub.add_parser(
        "check-catalog", help="verify approved apps exist on the remote"
    ).set_defaults(func=cmd_check_catalog)

    s = sub.add_parser("captive", help="open a temporary captive-portal window")
    s.add_argument("uid", type=int)
    s.add_argument("minutes", type=int, nargs="?", default=5)
    s.set_defaults(func=cmd_captive)

    s = sub.add_parser("guardian", help="manage the guardian password")
    s.add_argument("action", choices=["enable", "set-password", "disable"])
    s.set_defaults(func=cmd_guardian)

    s = sub.add_parser("update", help="check for or apply OS updates")
    s.add_argument("--apply", action="store_true")
    s.set_defaults(func=cmd_update)

    s = sub.add_parser("validate", help="[offline] validate a policy file")
    s.add_argument("policy")
    s.set_defaults(func=cmd_validate)

    s = sub.add_parser("render-nft", help="[offline] render the nftables ruleset for a policy file")
    s.add_argument("policy")
    s.add_argument("--dns-uid", type=int, default=0,
                   help="override the dnsmasq uid; by default "
                        "the one on this machine is used")
    s.set_defaults(func=cmd_render_nft)

    s = sub.add_parser("render-dnsmasq", help="[offline] render the dnsmasq drop-in for a policy file")
    s.add_argument("policy")
    s.set_defaults(func=cmd_render_dnsmasq)

    args = p.parse_args(argv)
    try:
        return args.func(args)
    except policy_mod.PolicyError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except Exception as e:  # noqa: BLE001
        # Daemon-side refusals arrive as GLib.GError like
        # "GDBus.Error:org.kosherlinux.Daemon1.Error: <message> (36)".
        if e.__class__.__module__.startswith("gi."):
            msg = re.sub(r"^.*?GDBus\.Error:[\w.]+: ", "", str(e))
            print(f"error: {re.sub(r' \(\d+\)$', '', msg)}", file=sys.stderr)
            return 1
        raise


if __name__ == "__main__":
    sys.exit(main())
