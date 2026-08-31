"""kosherctl — CLI for kosherd (admin/support/dev).

Online commands talk to the daemon over D-Bus (polkit will prompt).
Offline commands (validate / render-nft / render-dnsmasq) run the pure policy
engine locally — the fast dev loop for enforcement changes.
"""

from __future__ import annotations

import argparse
import getpass
import json
import re
import sys
from pathlib import Path

from . import dns, nft, policy as policy_mod


def _client():
    from .client import DaemonClient  # Gio import deferred: offline cmds need no GObject

    return DaemonClient()


def _guardian_pw(args) -> str:
    if getattr(args, "guardian_password", None):
        return args.guardian_password
    if _client().guardian_enabled():
        return getpass.getpass("Guardian password: ")
    return ""


def cmd_status(args) -> int:
    pol = _client().get_policy()
    print(f"policy revision {pol['revision']} (source: {pol['source']}, "
          f"guardian: {'on' if pol['guardian']['enabled'] else 'off'})")
    for u in pol["users"]:
        extra = f" whitelist={len(u.get('whitelist', []))} domains" if u["mode"] == "whitelist" else ""
        print(f"  {u['username']} (uid {u['uid']}): {u['mode']}{extra}")
    guest = pol.get("guest", {"enabled": False})
    if guest["enabled"]:
        print(f"  guest: enabled, mode {guest.get('mode', 'whitelist')}")
    else:
        print("  guest: disabled")
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


def cmd_profile(args) -> int:
    c = _client()
    if args.action == "list":
        for p in c.list_profiles():
            mark = " (default)" if p.get("default") else ""
            print(f"  {p['key']:<12} {p['label']}{mark}")
            print(f"               {p['description']}")
        return 0
    c.apply_profile(args.uid, args.profile, _guardian_pw(args))
    print(f"uid {args.uid} set to the {args.profile} profile")
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

    print("\nKosherOS setup\n")
    print("Create the administrator account for this computer.")
    print("It manages profiles, filtering and apps. It has no root access.\n")

    while True:
        username = input("Username: ").strip()
        if re.fullmatch(r"[a-z_][a-z0-9_-]*", username or ""):
            break
        print("  Use lowercase letters, digits, - or _ (starting with a letter).")

    full_name = input(f"Full name [{username}]: ").strip() or username

    while True:
        password = getpass.getpass("Password: ")
        if len(password) < 6:
            print("  At least 6 characters, please.")
            continue
        if password != getpass.getpass("Confirm password: "):
            print("  Those did not match.")
            continue
        break

    uid = c.create_first_admin(username, full_name, password)
    print(f"\nCreated {username} (uid {uid}).")

    guardian = ""
    if input("\nSet a guardian password (a second password required to "
             "change filter settings)? [y/N]: ").strip().lower().startswith("y"):
        while True:
            guardian = getpass.getpass("Guardian password: ")
            if len(guardian) >= 6 and guardian == getpass.getpass("Confirm: "):
                break
            print("  At least 6 characters, and both must match.")

    c.finish_setup(guardian, "")
    print("\nSetup complete. Starting the login screen.\n")
    return 0


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
        print(c.check_update())
    return 0


def _load_local_policy(path: str) -> policy_mod.Policy:
    return policy_mod.Policy.from_dict(json.loads(Path(path).read_text()))


def cmd_validate(args) -> int:
    _load_local_policy(args.policy)
    print(f"{args.policy}: valid")
    return 0


def cmd_render_nft(args) -> int:
    print(nft.render(_load_local_policy(args.policy), dns_uid=args.dns_uid), end="")
    return 0


def cmd_render_dnsmasq(args) -> int:
    print(dns.render(_load_local_policy(args.policy)), end="")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="kosherctl", description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="show managed users and their filter modes").set_defaults(func=cmd_status)
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

    s = sub.add_parser("profile", help="apply a ready-made profile to a user")
    s.add_argument("action", choices=["list", "set"])
    s.add_argument("uid", type=int, nargs="?", default=0)
    s.add_argument("profile", nargs="?", default="")
    s.add_argument("--guardian-password")
    s.set_defaults(func=cmd_profile)

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
    s.add_argument("--dns-uid", type=int, default=989)
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
