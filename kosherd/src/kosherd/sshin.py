"""Who may be signed in to over SSH: rendered from the policy.

sshd is enabled in the image, but its drop-in (sshd.service.d/
kosher-dev-only.conf) lets it start only where root has a key — a dev
disk — or where this file exists. kosherd writes it while at least one
account has "Sign in over SSH" on (an account with developer tools), and
removes it when none has, so a family machine that never turns it on has
no sshd at all.

sshd takes the first value it reads for a keyword, and Fedora's
sshd_config includes sshd_config.d/*.conf at the top, so this file, named
ahead of 50-redhat.conf, decides:

- keys only: a password would let anyone on the network, or another
  account on this computer through `ssh localhost`, try to guess one;
- the listed accounts and root (root is locked and has a key only on a
  dev disk);
- forwarding toward this computer only: `ssh -L` connects as the account,
  through its own filter, and is what a developer reaching a server they
  run here needs; `ssh -R` and a tunnel would open a listener here that
  reaches out from the other machine, unfiltered, for any account on this
  one.
"""

from __future__ import annotations

from pathlib import Path

from .policy import Policy

CONF_PATH = Path("/etc/ssh/sshd_config.d/40-kosheros.conf")
SERVICE = "sshd.service"


def allowed_users(policy: Policy) -> list[str]:
    """The accounts with "Sign in over SSH" on, in uid order."""
    return [u.username for u in sorted(policy.ssh_in_accounts(), key=lambda u: u.uid)]


def render(policy: Policy) -> str | None:
    """The sshd configuration, or None when nobody may sign in over SSH."""
    users = allowed_users(policy)
    if not users:
        return None
    return (
        f"# Written by kosherd from policy revision {policy.revision}. DO NOT EDIT.\n"
        "# Who may sign in over SSH: the accounts with developer tools and\n"
        "# \"Sign in over SSH\" on, with a key in their own ~/.ssh/authorized_keys.\n"
        f"AllowUsers root {' '.join(users)}\n"
        "PasswordAuthentication no\n"
        "KbdInteractiveAuthentication no\n"
        "PermitEmptyPasswords no\n"
        "PermitRootLogin prohibit-password\n"
        "AllowTcpForwarding local\n"
        "AllowStreamLocalForwarding local\n"
        "GatewayPorts no\n"
        "PermitTunnel no\n"
        "X11Forwarding no\n"
    )
