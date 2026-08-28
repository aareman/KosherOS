"""Turn a policy into running enforcement: nftables ruleset + dnsmasq drop-in.

Write-then-load, so /etc/kosher/nft/kosher.nft always holds the last applied
ruleset and kosher-firewall.service re-establishes it on the next boot before
the network comes up (fail-closed by unit ordering).
"""

from __future__ import annotations

import json
import logging
import os
import pwd
import subprocess
import tempfile
from pathlib import Path

from . import dns, mitmca, nft
from .policy import Policy

log = logging.getLogger(__name__)

NFT_RULESET_PATH = Path("/etc/kosher/nft/kosher.nft")
DNSMASQ_DROPIN_PATH = Path(dns.WHITELIST_CONF)
DNS_SERVICE = "kosher-dns.service"
MITM_SERVICE = "kosher-mitm.service"
# The proxy runs unprivileged and cannot traverse /var/lib/kosher
# (root-only: it holds the policy), so it gets its own state dir.
MITM_DIR = Path("/var/lib/kosher-mitm")
MITM_RULES_PATH = MITM_DIR / "rules.json"


class ApplyError(Exception):
    pass


def dnsmasq_uid() -> int:
    for name in ("dnsmasq", "nobody"):
        try:
            return pwd.getpwnam(name).pw_uid
        except KeyError:
            continue
    raise ApplyError("no dnsmasq user found")


def mitm_uid() -> int | None:
    try:
        return pwd.getpwnam("kosher-mitm").pw_uid
    except KeyError:
        return None


def write_mitm_rules(policy: Policy) -> None:
    """Render just the URL rules for the unprivileged proxy to read.

    The proxy never sees the policy itself — only uid -> rules, group
    readable by kosher-mitm.
    """
    rules = {
        str(user.uid): user.rules
        for user in policy.effective_users()
        if user.mode == "inspect" and user.rules
    }
    MITM_DIR.mkdir(parents=True, exist_ok=True)
    _write_atomic(MITM_RULES_PATH, json.dumps(rules, indent=2) + "\n", mode=0o644)


def _write_atomic(path: Path, content: str, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.rename(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise


def apply_policy(policy: Policy) -> None:
    """Render and load enforcement for `policy`. Raises ApplyError on failure."""
    ruleset = nft.render(policy, dns_uid=dnsmasq_uid(), mitm_uid=mitm_uid())

    # Syntax-check before touching the live ruleset or the boot file.
    with tempfile.NamedTemporaryFile("w", suffix=".nft") as check:
        check.write(ruleset)
        check.flush()
        res = subprocess.run(["nft", "--check", "-f", check.name], capture_output=True, text=True)
        if res.returncode != 0:
            raise ApplyError(f"rendered ruleset failed nft --check: {res.stderr}")

    _write_atomic(NFT_RULESET_PATH, ruleset, mode=0o600)
    res = subprocess.run(["nft", "-f", str(NFT_RULESET_PATH)], capture_output=True, text=True)
    if res.returncode != 0:
        raise ApplyError(f"nft load failed: {res.stderr}")

    write_mitm_rules(policy)
    inspected = any(u.mode == "inspect" for u in policy.effective_users())
    if inspected:
        # Inspection needs the machine to trust the proxy's CA, or every
        # HTTPS page would warn. Generated once, on first use.
        try:
            mitmca.ensure_ca()
        except Exception:  # noqa: BLE001 - never leave the firewall unapplied
            log.exception("could not prepare the inspection CA")
    subprocess.run(
        ["systemctl", "restart" if inspected else "stop", MITM_SERVICE],
        capture_output=True, text=True,
    )

    _write_atomic(DNSMASQ_DROPIN_PATH, dns.render(policy))
    # Full restart, not reload: dnsmasq's SIGHUP re-reads /etc/hosts and clears
    # the cache but does NOT re-read config files, so new nftset= directives
    # would never take effect. Restart also flushes cached answers, which is
    # what we want after a whitelist change.
    res = subprocess.run(
        ["systemctl", "restart", DNS_SERVICE], capture_output=True, text=True
    )
    if res.returncode != 0:
        # DNS reload failure must not leave us silently unfiltered; the nft
        # rules are already live, so log loudly but do not roll back.
        log.error("failed to reload %s: %s", DNS_SERVICE, res.stderr)

    log.info("applied policy revision %d", policy.revision)
