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
import logging
import subprocess
from pathlib import Path

from .policy import Policy

log = logging.getLogger(__name__)

CONF_PATH = Path("/etc/nix/kosheros-users.conf")
# The build users' group, as the nix packages create it (sysusers).
BUILD_GROUP = "nixbld"
# The group that may run /usr/bin/nix (the image's sysusers.d/kosher-nix.conf).
CLI_GROUP = "kosher-nix"


def build_gid() -> int | None:
    """The nixbld group's gid, or None where Nix is not installed."""
    try:
        return grp.getgrnam(BUILD_GROUP).gr_gid
    except KeyError:
        return None


def allowed_users(policy: Policy) -> list[str]:
    """Usernames that may talk to the daemon: root, then the accounts an
    administrator switched developer tools on for (#123), in uid order.

    It used to be every account with internet, which let any supervised
    account install anything from nixpkgs from a shell and run builds as
    the build users, outside its own filter. The Store's installs still
    reach everyone: kosherd runs them as root into the account's profile.
    """
    names = ["root"]
    for user in sorted(policy.developer_accounts(), key=lambda u: u.uid):
        if user.username not in names:
            names.append(user.username)
    return names


def cli_members(policy: Policy) -> list[str]:
    """Who may run the nix program at all: the accounts with developer
    tools (#123). Root needs no membership.

    allowed-users only covers the daemon, and Nix does not need the daemon:
    with a store of its own under ~ (`--store ~/x`, or `store = ...` in
    ~/.config/nix/nix.conf or NIX_CONFIG) it downloads and builds by
    itself, and any account could install anything from nixpkgs that way
    (#214). So the image makes /usr/bin/nix and the libraries it is built
    from readable by root and CLI_GROUP alone, and kosherd keeps that
    group to these accounts.
    """
    return [u.username for u in sorted(policy.developer_accounts(), key=lambda u: u.uid)]


def sync_cli_group(policy: Policy, run=subprocess.run) -> bool:
    """Make CLI_GROUP's members exactly cli_members(policy). Returns whether
    it changed anything. A machine without the group (an image before
    #214, or no Nix) is left alone.

    The kernel reads a process's groups when the person signs in, so a
    session that is already open keeps the groups it started with until
    its next sign-in; a new runuser (kosherd's own installs) sees the
    change at once.
    """
    try:
        current = grp.getgrnam(CLI_GROUP).gr_mem
    except KeyError:
        return False
    wanted = cli_members(policy)
    if sorted(current) == sorted(wanted):
        return False
    res = run(["gpasswd", "-M", ",".join(wanted), CLI_GROUP],
              capture_output=True, text=True)
    if res.returncode != 0:
        log.warning("could not set the members of %s: %s", CLI_GROUP,
                    (res.stderr or "").strip())
        return False
    log.info("%s is now: %s", CLI_GROUP, ", ".join(wanted) or "nobody")
    return True


def render(policy: Policy) -> str:
    return (
        f"# Written by kosherd from policy revision {policy.revision}. DO NOT EDIT.\n"
        "# Included by /etc/nix/nix.conf: who may use the Nix daemon — root and\n"
        "# the accounts with developer tools switched on (#123).\n"
        f"allowed-users = {' '.join(allowed_users(policy))}\n"
    )
