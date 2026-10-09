"""Who may use the Nix daemon: rendered from the policy.

Nix is on every KosherOS machine (issue #34). Its daemon runs as root and
builds on behalf of whoever asks, so "who may ask" is a filter decision
like any other: every managed account whose kind of internet is not
"No internet". The shipped /etc/nix/nix.conf allows root alone and ends
with `!include` of the file written here, so a machine kosherd has not
yet configured — or an account it does not manage — gets nothing.

The daemon reads its configuration when it starts, so a changed list
means a restart; apply.py restarts it only when the list changed, and
only if it is running (a build in flight on an unchanged list is left
alone).
"""

from __future__ import annotations

import grp
from pathlib import Path

from .policy import Policy

CONF_PATH = Path("/etc/nix/kosheros-users.conf")
# The build users' group, as the nix packages create it (sysusers).
BUILD_GROUP = "nixbld"


def build_gid() -> int | None:
    """The nixbld group's gid, or None where Nix is not installed."""
    try:
        return grp.getgrnam(BUILD_GROUP).gr_gid
    except KeyError:
        return None


def allowed_users(policy: Policy) -> list[str]:
    """Usernames that may talk to the daemon: root, then every managed
    account with some kind of internet, in uid order."""
    names = ["root"]
    for user in sorted(policy.effective_users(), key=lambda u: u.uid):
        if user.mode != "none" and user.username not in names:
            names.append(user.username)
    return names


def render(policy: Policy) -> str:
    return (
        f"# Written by kosherd from policy revision {policy.revision}. DO NOT EDIT.\n"
        "# Included by /etc/nix/nix.conf: who may use the Nix daemon. Every\n"
        "# managed account whose kind of internet is not \"No internet\".\n"
        f"allowed-users = {' '.join(allowed_users(policy))}\n"
    )
