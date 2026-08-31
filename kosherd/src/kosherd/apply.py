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

from . import categories as categories_mod
from . import dns, mitmca, nft
from .policy import INSPECTED_MODES, SAFESEARCH_MODES, UNFILTERED_MODES, Policy

log = logging.getLogger(__name__)

NFT_RULESET_PATH = Path("/etc/kosher/nft/kosher.nft")
DNSMASQ_DROPIN_PATH = Path(dns.WHITELIST_CONF)
SAFESEARCH_PATH = Path(dns.SAFESEARCH_CONF)
CATEGORY_BLOCK_PATH = Path(dns.CATEGORY_BLOCK_CONF)
DNS_SERVICE = "kosher-dns.service"
# Only runs while somebody is unfiltered (see nft.OPEN_DNS_PORT).
OPEN_DNS_SERVICE = "kosher-dns-open.service"
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
    """Render what the unprivileged proxy needs: per-uid rules and categories.

    The proxy never sees the policy itself — only what it must enforce,
    group readable by kosher-mitm.
    """
    per_user = {}
    for user in policy.effective_users():
        if user.mode not in INSPECTED_MODES:
            continue
        if not (user.rules or user.blocked_categories
                or user.media_level != "none" or user.youtube):
            continue
        per_user[str(user.uid)] = {
            "rules": user.rules,
            "blocked_categories": user.blocked_categories,
            "media_level": user.media_level,
            "youtube": user.youtube,
        }
    MITM_DIR.mkdir(parents=True, exist_ok=True)
    _write_atomic(MITM_RULES_PATH, json.dumps(per_user, indent=2) + "\n", mode=0o644)


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


def _restart(service: str):
    """Restart a service, clearing any previous failure first.

    These services are restarted on every policy change. If one has already
    tripped systemd's start rate limit, plain `restart` is refused and it
    stays down — for the resolver that means no DNS at all.
    """
    subprocess.run(["systemctl", "reset-failed", service],
                   capture_output=True, text=True)
    return subprocess.run(["systemctl", "restart", service],
                          capture_output=True, text=True)


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
    inspected = any(u.mode == "filtered" for u in policy.effective_users())
    if inspected:
        # Inspection needs the machine to trust the proxy's CA, or every
        # HTTPS page would warn. Generated once, on first use.
        try:
            mitmca.ensure_ca()
        except Exception:  # noqa: BLE001 - never leave the firewall unapplied
            log.exception("could not prepare the inspection CA")
    _restart(MITM_SERVICE) if inspected else subprocess.run(
        ["systemctl", "stop", MITM_SERVICE], capture_output=True, text=True)

    _write_atomic(DNSMASQ_DROPIN_PATH, dns.render(policy))
    _write_atomic(SAFESEARCH_PATH, dns.render_safesearch(policy))
    _write_atomic(CATEGORY_BLOCK_PATH,
                  dns.render_category_blocks(policy, categories_mod.load()))

    # The plain resolver exists only for unfiltered users; running it when
    # nobody is unfiltered would just be an unfiltered resolver sitting on
    # the machine.
    unfiltered = any(u.mode in UNFILTERED_MODES for u in policy.effective_users())
    if unfiltered:
        _restart(OPEN_DNS_SERVICE)
    else:
        subprocess.run(["systemctl", "stop", OPEN_DNS_SERVICE],
                       capture_output=True, text=True)
    # Full restart, not reload: dnsmasq's SIGHUP re-reads /etc/hosts and clears
    # the cache but does NOT re-read config files, so new nftset= directives
    # would never take effect. Restart also flushes cached answers, which is
    # what we want after a whitelist change.
    res = _restart(DNS_SERVICE)
    if res.returncode != 0:
        # DNS reload failure must not leave us silently unfiltered; the nft
        # rules are already live, so log loudly but do not roll back.
        log.error("failed to reload %s: %s", DNS_SERVICE, res.stderr)

    log.info("applied policy revision %d", policy.revision)
