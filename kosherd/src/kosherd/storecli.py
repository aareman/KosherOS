"""`kosher`: the Store from a terminal, in the words apt and dnf taught.

    kosher search <words>       what this account may install, matching the words
    kosher install <app>...     install, by name or id, and watch it happen
    kosher remove <app>...      remove (an administrator's action)
    kosher list                 what is installed
    kosher update               ask the store for news and say what has an update
    kosher upgrade [<app>...]   take the updates, all of them or the named ones
    kosher info <app>           what the Store knows about an app

`kosherctl store` is the operator's tool and says exactly what the daemon
did; this is for a person who types `kosher install claude-code` and
expects it to work. Both talk to the same daemon with the same rules: an
account installs what it may have, and a nixpkgs app lands in its own
profile. An app may be named by its Store id (org.gnome.Chess,
nixpkgs#gh), by its name ("Claude Code"), or by enough of either to be
unambiguous (chess, claude).
"""

from __future__ import annotations

import argparse
import sys
from typing import Callable

PROG = "kosher"


def _client():
    from .client import DaemonClient  # Gio deferred, as in cli.py

    return DaemonClient()


def _said(error: Exception) -> str:
    """The daemon's sentence out of a D-Bus error, without the plumbing."""
    text = getattr(error, "message", None) or str(error)
    for marker in ("org.kosherlinux.Daemon1.Error: ", "GDBus.Error:"):
        if marker in text:
            text = text.split(marker, 1)[1]
    return text.strip()


def _words(app: dict) -> str:
    return app.get("name") or app["ref"]


# -- naming an app -------------------------------------------------------------

def resolve(word: str, apps: list[dict]) -> dict | list[dict]:
    """The one app `word` means, or the candidates when it means several
    (an empty list when it means none).

    Exact id, then exact name, then the last part of the id (org.gnome.Chess
    is "chess"; nixpkgs#gh is "gh"), then anything whose name or id
    contains the word. Case does not matter.
    """
    w = word.strip().lower()
    if not w:
        return []
    by_ref = [a for a in apps if a["ref"].lower() == w]
    if by_ref:
        return by_ref[0]
    by_name = [a for a in apps if (a.get("name") or "").lower() == w]
    if len(by_name) == 1:
        return by_name[0]
    by_tail = [a for a in apps
               if a["ref"].lower().rsplit(".", 1)[-1].rsplit("#", 1)[-1] == w]
    if len(by_tail) == 1:
        return by_tail[0]
    loose = [a for a in apps
             if w in a["ref"].lower() or w in (a.get("name") or "").lower()]
    if len(loose) == 1:
        return loose[0]
    return by_name or by_tail or loose


def _pick(word: str, apps: list[dict]) -> dict | None:
    found = resolve(word, apps)
    if isinstance(found, dict):
        return found
    if not found:
        print(f"nothing called {word!r}; try: kosher search {word}", file=sys.stderr)
    else:
        print(f"{word!r} could be any of these; say which:", file=sys.stderr)
        for app in found[:12]:
            print(f"  {app['ref']:<40} {_words(app)}", file=sys.stderr)
    return None


# -- watching a job ---------------------------------------------------------------

def _wait_for(client, refs: list[str], out=sys.stdout) -> bool:
    """Show progress for `refs` until the daemon has finished each. True
    when every one succeeded. Needs a GLib main loop, so it is here and
    not in the commands, which the tests drive with a stand-in."""
    from gi.repository import GLib

    loop = GLib.MainLoop()
    pending = set(refs)
    outcome: dict[str, tuple[bool, str]] = {}
    last = {}

    def on_progress(ref, percent, status):
        if ref in pending and (percent, status) != last.get(ref):
            last[ref] = (percent, status)
            print(f"\r  {ref}: {percent:3d}% {status:<40}", end="", file=out, flush=True)

    def on_finished(ref, ok, error):
        if ref not in pending:
            return
        pending.discard(ref)
        outcome[ref] = (ok, error)
        print(f"\r  {ref}: {'done' if ok else 'failed: ' + error:<50}", file=out)
        if not pending:
            loop.quit()

    client.connect_app_signals(on_progress, on_finished)
    try:
        loop.run()
    except KeyboardInterrupt:
        print("\nstill running in the daemon; `kosher list` will show it when it lands", file=out)
        return False
    return all(ok for ok, _ in outcome.values())


# -- the commands -------------------------------------------------------------

def cmd_search(args, client, wait=_wait_for) -> int:
    store = client.list_store_apps()
    needle = " ".join(args.words).strip().lower()
    hits = [a for a in store.get("apps", [])
            if not needle or needle in (a.get("name") or "").lower()
            or needle in a["ref"].lower() or needle in (a.get("summary") or "").lower()]
    if not hits:
        print(f"nothing in the Store for this account matches {needle!r}")
        if not store.get("ready", True):
            print("the rest of the store is still being downloaded")
        return 1
    for app in sorted(hits, key=lambda a: _words(a).lower()):
        print(f"  {app['ref']:<40} {_words(app):<24} {app.get('summary') or ''}")
    return 0


def cmd_info(args, client, wait=_wait_for) -> int:
    apps = client.list_store_apps().get("apps", [])
    app = _pick(args.app, apps)
    if app is None:
        return 1
    print(f"{_words(app)}  ({app['ref']})")
    if app.get("summary"):
        print(f"  {app['summary']}")
    source = "Nix, into your own profile" if app["ref"].startswith("nixpkgs#") else "Flatpak, from Flathub"
    print(f"  how: {source}")
    if app.get("kind"):
        print(f"  shelf: {app['kind']}")
    installed = set(client.list_installed())
    print("  installed: " + ("yes" if app["ref"] in installed else "no"))
    return 0


def cmd_list(args, client, wait=_wait_for) -> int:
    try:
        details = client.list_installed_details()
    except Exception:  # noqa: BLE001 - an older daemon has only the refs
        details = [{"ref": r, "name": r} for r in client.list_installed()]
    if not details:
        print("nothing is installed from the Store")
        return 0
    for app in sorted(details, key=lambda a: _words(a).lower()):
        by = app.get("installed_by") or ""
        print(f"  {app['ref']:<40} {_words(app):<24} {by}")
    return 0


def cmd_install(args, client, wait=_wait_for) -> int:
    apps = client.list_store_apps().get("apps", [])
    installed = set(client.list_installed())
    refs = []
    for word in args.apps:
        app = _pick(word, apps)
        if app is None:
            return 1
        if app["ref"] in installed:
            print(f"{_words(app)} is already installed")
            continue
        refs.append(app["ref"])
    if not refs:
        return 0
    for ref in refs:
        try:
            client.install_app(ref)
        except Exception as e:  # noqa: BLE001 - the daemon's refusal is the message
            print(f"{ref}: {_said(e)}", file=sys.stderr)
            return 1
        print(f"installing {ref}")
    return 0 if wait(client, refs) else 1


def cmd_remove(args, client, wait=_wait_for) -> int:
    try:
        known = {a["ref"]: a for a in client.list_installed_details()}
    except Exception:  # noqa: BLE001
        known = {r: {"ref": r} for r in client.list_installed()}
    refs = []
    for word in args.apps:
        app = _pick(word, list(known.values()))
        if app is None:
            return 1
        refs.append(app["ref"])
    for ref in refs:
        try:
            client.remove_app(ref)
        except Exception as e:  # noqa: BLE001
            print(f"{ref}: {_said(e)}", file=sys.stderr)
            return 1
        print(f"removing {ref}")
    return 0 if wait(client, refs) else 1


def cmd_update(args, client, wait=_wait_for) -> int:
    found = client.check_app_updates()
    if not found:
        print("everything installed from the Store is up to date")
        return 0
    for app in found:
        version = app.get("version") or app.get("commit") or ""
        latest = app.get("latest") or ""
        print(f"  {app['ref']:<40} {_words(app):<24} {version}" + (f" -> {latest}" if latest else ""))
    print(f"{len(found)} can be upgraded: kosher upgrade")
    return 0


def cmd_upgrade(args, client, wait=_wait_for) -> int:
    found = client.list_app_updates()
    if args.apps:
        refs = []
        for word in args.apps:
            app = _pick(word, found)
            if app is None:
                return 1
            refs.append(app["ref"])
    else:
        refs = [a["ref"] for a in found]
    if not refs:
        print("nothing to upgrade (kosher update asks the store for news)")
        return 0
    for ref in refs:
        try:
            client.update_app(ref)
        except Exception as e:  # noqa: BLE001
            print(f"{ref}: {_said(e)}", file=sys.stderr)
            return 1
        print(f"upgrading {ref}")
    return 0 if wait(client, refs) else 1


COMMANDS: dict[str, Callable] = {
    "search": cmd_search, "install": cmd_install, "remove": cmd_remove,
    "list": cmd_list, "update": cmd_update, "upgrade": cmd_upgrade, "info": cmd_info,
}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog=PROG, description="The KosherOS Store, from a terminal.")
    sub = p.add_subparsers(dest="command", required=True)
    s = sub.add_parser("search", help="what this account may install, matching the words")
    s.add_argument("words", nargs="*")
    s = sub.add_parser("install", help="install apps, by name or id, and watch it happen")
    s.add_argument("apps", nargs="+")
    s = sub.add_parser("remove", help="remove apps (an administrator's action)")
    s.add_argument("apps", nargs="+")
    sub.add_parser("list", help="what is installed")
    sub.add_parser("update", help="ask the store for news and say what has an update")
    s = sub.add_parser("upgrade", help="take the updates: all of them, or the named apps")
    s.add_argument("apps", nargs="*")
    s = sub.add_parser("info", help="what the Store knows about an app")
    s.add_argument("app")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return COMMANDS[args.command](args, _client(), wait=_wait_for)
    except Exception as e:  # noqa: BLE001 - a D-Bus refusal, said plainly
        if e.__class__.__name__ in ("Error", "GError"):
            print(_said(e), file=sys.stderr)
            return 1
        raise


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
