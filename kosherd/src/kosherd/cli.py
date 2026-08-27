"""kosherctl — CLI for kosherd (admin/support/dev).

Online commands talk to the daemon over D-Bus (polkit will prompt).
Offline commands (validate / render-nft / render-dnsmasq) run the pure policy
engine locally — the fast dev loop for enforcement changes.
"""

from __future__ import annotations

import argparse
import getpass
import json
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


def cmd_create_child(args) -> int:
    uid = _client().create_child(args.username, args.full_name or args.username, args.mode)
    print(f"created {args.username} (uid {uid}, mode {args.mode})")
    return 0


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

    s = sub.add_parser("create-child", help="create a managed child account")
    s.add_argument("username")
    s.add_argument("mode", choices=policy_mod.MODES)
    s.add_argument("--full-name")
    s.set_defaults(func=cmd_create_child)

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


if __name__ == "__main__":
    sys.exit(main())
