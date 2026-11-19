"""Turn a policy into running enforcement: nftables ruleset + dnsmasq drop-in.

Write-then-load, so /etc/kosher/nft/kosher.nft always holds the last applied
ruleset and kosher-firewall.service re-establishes it on the next boot before
the network comes up (fail-closed by unit ordering).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import pwd
import subprocess
import tempfile
from pathlib import Path

from . import categories as categories_mod
from . import (buildsources, containers, dns, lists, mitmca, nft, nixdaemon,
               paymentsecurity, sshin, syncthing, tailnet, whitelists)
from . import search as search_mod
from .policy import DEVELOPER_REGISTRIES, INSPECTED_MODES, PROXIED_MODES, SAFESEARCH_MODES, UNFILTERED_MODES, Policy

log = logging.getLogger(__name__)

NFT_RULESET_PATH = Path("/etc/kosher/nft/kosher.nft")
DNSMASQ_DROPIN_PATH = Path(dns.WHITELIST_CONF)
SAFESEARCH_PATH = Path(dns.SAFESEARCH_CONF)
CATEGORY_BLOCK_PATH = Path(dns.CATEGORY_BLOCK_CONF)
LEGACY_CATEGORY_BLOCK_PATH = Path(dns.LEGACY_CATEGORY_BLOCK_CONF)
ADBLOCK_PATH = Path(dns.ADBLOCK_CONF)
ADBLOCK_OPEN_PATH = Path(dns.ADBLOCK_OPEN_CONF)
DNS_SERVICE = "kosher-dns.service"
# Only runs while somebody is unfiltered (see nft.OPEN_DNS_PORT).
OPEN_DNS_SERVICE = "kosher-dns-open.service"
# Only runs while somebody is on dnsfilter (see nft.DNSFILTER_DNS_PORT).
DNSFILTER_DNS_SERVICE = "kosher-dns-dnsfilter.service"
MITM_SERVICE = "kosher-mitm.service"
# The proxy runs unprivileged and cannot traverse /var/lib/kosher
# (root-only: it holds the policy), so it gets its own state dir.
MITM_DIR = Path("/var/lib/kosher-mitm")
MITM_RULES_PATH = MITM_DIR / "rules.json"
# The search front end and the metasearch engine behind it, likewise
# unprivileged and likewise given only what they filter with.
SEARCH_SERVICES = ("kosher-searxng.service", "kosher-search.service")
SEARCH_FRONT_END = "kosher-search.service"
# What each restarted-on-demand service was last started with (see
# _restart_if_changed). In kosherd's own state directory.
STARTED_DIR = Path("/var/lib/kosher/started")
SEARCH_DIR = Path("/var/lib/kosher-search")
SEARCH_POLICY_PATH = Path(search_mod.SEARCH_POLICY_PATH)
NIX_DAEMON_SERVICE = "nix-daemon.service"
NIX_USERS_PATH = nixdaemon.CONF_PATH


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


def channel_search_uid() -> int | None:
    try:
        return pwd.getpwnam("kosher-ytsearch").pw_uid
    except KeyError:
        return None


def write_mitm_rules(policy: Policy, tailnet_allowed: tuple[str, ...] | None = None) -> None:
    """Render what the unprivileged proxy needs: per-uid rules and categories.

    The proxy never sees the policy itself — only what it must enforce,
    group readable by kosher-mitm.

    `system_allowed` is what no category blocks for anybody: Tailscale's
    sites and the tailnet's names while it is set up (tailnet.allowance,
    #211). Only categories: rules, picture and text scoring still apply.
    """
    if tailnet_allowed is None:
        tailnet_allowed = tailnet.allowed()
    ports = nft.mitm_ports(policy)
    per_user = {}
    for user in policy.effective_users():
        if user.mode not in PROXIED_MODES:
            continue
        # Every inspected user is listed, even one with nothing configured:
        # the entry carries the port the proxy identifies them by. (The
        # proxy still fails closed for a port it has no entry for.)
        per_user[str(user.uid)] = {
            "port": ports[user.uid],
            "lan_hosts": list(user.lan_hosts),
            "rules": user.rules,
            "blocked_categories": user.blocked_categories,
            "media_level": user.media_level,
            "cover_style": getattr(user, "cover_style", "frost"),
            "language_filter": getattr(user, "language_filter", "off"),
            "youtube": user.youtube,
            "calm": getattr(user, "calm", {}),
            "system_allowed": list(tailnet_allowed),
            # Payment and fraud-check hosts a checkout needs, which the
            # tracker lists carry (#265): no category blocks them, for
            # every filtered account. Same standing as system_allowed in
            # the proxy; a key of its own so the reason is on record.
            "never_blocked": paymentsecurity.combined(policy.adblock_allowed),
        }
        if user.mode not in INSPECTED_MODES:
            # No content inspection or path rules in name-only modes. The
            # family list is per account; machine update/CDN names are not
            # web permissions for the person at the keyboard (F5).
            entry = per_user[str(user.uid)]
            entry.update(mode=user.mode, rules=[], media_level="none",
                         language_filter="off", youtube={}, calm={},
                         allowed_hosts=[*whitelists.effective(user),
                                        *(DEVELOPER_REGISTRIES if user.developer_tools else ()),
                                        *tailnet_allowed],
                         lan_hosts=list(user.lan_hosts))
    listen_ports = sorted(ports.values())
    if channel_search_uid() is not None:
        per_user["channel-search"] = {
            "port": nft.CHANNEL_SEARCH_PORT, "mode": "filtered",
            "rules": [], "blocked_categories": [], "media_level": "none",
            "cover_style": "frost", "language_filter": "off", "youtube": {},
            "calm": {},
        }
        listen_ports.append(nft.CHANNEL_SEARCH_PORT)
    if nixdaemon.build_gid() is not None:
        # The Nix build users (#123): a supervised identity of their own,
        # with bare addresses and DoH refused and no picture or text
        # scoring, since what a build fetches is a tarball, not a page.
        # What else it may fetch is the strictest filter among the
        # developer accounts, the only ones that can start a build (#199,
        # buildsources.py); with none, nothing.
        per_user["builder"] = {
            "port": nft.BUILDER_PORT,
            "developers": buildsources.developer_filters(policy),
            # Judged per developer account above, not here.
            "rules": [],
            "blocked_categories": [],
            "media_level": "none",
            "cover_style": "frost",
            "language_filter": "off",
            "youtube": {},
            "calm": {},
        }
        listen_ports.append(nft.BUILDER_PORT)
    MITM_DIR.mkdir(parents=True, exist_ok=True)
    _write_atomic(MITM_RULES_PATH, json.dumps(per_user, indent=2) + "\n", mode=0o644)
    # The listener list, one transparent mode per port. systemd splits the
    # unquoted $MITM_MODES on whitespace into separate arguments.
    modes = " ".join(f"--mode transparent@{host}:{port}"
                     for port in sorted(listen_ports) for host in ("127.0.0.1", "::1"))
    # Beside the rules file (not a fixed path), so a relocated rules file in
    # a test or a future layout takes its listener list with it. Read by
    # kosher-mitm.service as EnvironmentFile.
    _write_atomic(MITM_RULES_PATH.with_name("listen.env"),
                  f"MITM_MODES={modes}\n", mode=0o644)


def search_uid() -> int | None:
    try:
        return pwd.getpwnam("kosher-search").pw_uid
    except KeyError:
        return None


def write_search_policy(policy: Policy) -> None:
    SEARCH_DIR.mkdir(parents=True, exist_ok=True)
    _write_atomic(SEARCH_POLICY_PATH, search_mod.render_policy(policy),
                  mode=0o644)


def write_nix_users(policy: Policy) -> bool:
    """Tell the Nix daemon who may use it (nixdaemon.py). Returns whether
    the list changed, in which case the daemon was told to pick it up.

    Nothing happens on a machine without Nix (no /etc/nix): the stage-1
    dev VM, or an image built without it.
    """
    path = NIX_USERS_PATH
    if not path.parent.is_dir():
        return False
    # And who may run nix at all (#214): without the daemon, an account
    # can still build into a store of its own.
    nixdaemon.sync_cli_group(policy)
    content = nixdaemon.render(policy)
    try:
        if path.read_text() == content:
            return False
    except OSError:
        pass
    _write_atomic(path, content, mode=0o644)
    # try-restart: a daemon that is not running (socket-activated, nobody
    # has used Nix yet) reads the new file when it starts; one that is
    # running must be restarted to see it.
    subprocess.run(["systemctl", "try-restart", NIX_DAEMON_SERVICE],
                   capture_output=True, text=True)
    return True


def write_ssh_in(policy: Policy, run=subprocess.run) -> bool:
    """Tell sshd who may sign in (sshin.py). Returns whether anything
    changed, in which case sshd was restarted: started for the first
    account, stopped with the last unless root has a key (a dev disk).
    Sessions already open are their own scopes and survive a restart.
    A machine without sshd's drop-in directory is left alone.
    """
    path = sshin.CONF_PATH
    if not path.parent.is_dir():
        return False
    content = sshin.render(policy)
    try:
        current = path.read_text()
    except OSError:
        current = None
    if current == content:
        return False
    if content is None:
        path.unlink(missing_ok=True)
    else:
        # sshd refuses a group- or world-writable file; 0600 like the rest.
        _write_atomic(path, content, mode=0o600)
    # restart, not try-restart: the unit's condition is what starts or
    # stops it, and a stopped sshd must start for the first account.
    res = run(["systemctl", "restart", sshin.SERVICE], capture_output=True, text=True)
    if res.returncode != 0:
        log.warning("could not restart sshd: %s", (res.stderr or "").strip())
    return True


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


def _lists_fingerprint() -> str:
    """What the proxy and the search service load once, when they start:
    the family's list edits and any category bundle delivered since the
    image (both under lists.OVERRIDE_DIR). The shipped lists change only
    with the image, which restarts everything."""
    digest = hashlib.sha256()
    root = lists.OVERRIDE_DIR
    if root.is_dir():
        for path in sorted(root.rglob("*")):
            try:
                if path.is_file():
                    st = path.stat()
                    digest.update(f"{path}:{st.st_mtime_ns}:{st.st_size}\n".encode())
            except OSError:
                continue
    return digest.hexdigest()


def _file_stamp(path: Path) -> str:
    try:
        return path.read_text()
    except OSError:
        return ""


def _connection_policy_stamp() -> str:
    """What an already-open proxy stream cannot safely learn by rereading.

    Name-only TLS is passed through after its first ClientHello. Removing
    an approved host, tightening categories or switching to full inspection
    must close that stream, even when the listener ports did not change.
    A LAN permission also bounds existing upstream connections/downloads.
    Ordinary filtered-page rules and visual settings remain live-reloaded.
    """
    doc = json.loads(MITM_RULES_PATH.read_text())
    connections = {}
    for uid, entry in doc.items():
        mode = entry.get("mode", "filtered")
        connection = {"mode": mode, "port": entry.get("port"),
                      "lan_hosts": sorted(set(entry.get("lan_hosts", ())))}
        if mode in PROXIED_MODES and mode not in INSPECTED_MODES:
            for key in ("allowed_hosts", "blocked_categories", "system_allowed", "never_blocked"):
                connection[key] = sorted(set(entry.get(key, ())))
        connections[uid] = connection
    return json.dumps(connections, sort_keys=True, separators=(",", ":"))


def _listener_identities_changed(policy: Policy) -> bool:
    """Whether nft would hand a listener to a different proxy identity.

    Listener ports are allocated by mode and UID, so an account addition,
    removal or mode change can reassign an existing port. The old proxy
    must be stopped before nft permits its new owner to reach that port.
    """
    wanted = {port: str(uid) for uid, port in nft.mitm_ports(policy).items()}
    if nixdaemon.build_gid() is not None:
        wanted[nft.BUILDER_PORT] = "builder"
    if channel_search_uid() is not None:
        wanted[nft.CHANNEL_SEARCH_PORT] = "channel-search"
    try:
        doc = json.loads(MITM_RULES_PATH.read_text())
    except FileNotFoundError:
        doc = {}
    except (OSError, ValueError):
        return True
    if not isinstance(doc, dict):
        return True
    previous = {}
    for uid, entry in doc.items():
        if not isinstance(entry, dict) or type(entry.get("port")) is not int:
            return True
        port = entry["port"]
        if port in previous:
            return True
        previous[port] = uid
    return previous != wanted


def _restart_if_changed(service: str, fingerprint: str):
    """Restart `service` only when what it reads at start has changed since
    kosherd last started it, or when it is not running; otherwise leave it.

    Every policy change used to restart the proxy and both search services.
    The proxy loads an image model, so each restart cost seconds before
    kosherd could answer, and for those seconds every account's web was
    down. Both re-read their policy file when it changes (rules.json,
    policy.json). The proxy must additionally close TLS passthrough streams
    when connection-level permissions change: those streams have no later
    HTTP hooks. Listener/list and connection-policy fingerprints are kept
    in STARTED_DIR.
    """
    stamp = STARTED_DIR / service
    running = subprocess.run(["systemctl", "is-active", "--quiet", service],
                             capture_output=True, text=True).returncode == 0
    # A missing stamp is "never started by kosherd", even for an empty
    # fingerprint.
    if running and stamp.exists() and _file_stamp(stamp) == fingerprint:
        return None
    res = _restart(service)
    if res.returncode == 0:
        # Best effort: a stamp that cannot be written costs one more
        # restart next time, never the apply.
        try:
            _write_atomic(stamp, fingerprint, mode=0o600)
        except OSError as e:
            log.warning("could not note what %s started with: %s", service, e)
    return res


def apply_policy(policy: Policy) -> None:
    """Render and load enforcement for `policy`. Raises ApplyError on failure."""
    # What Tailscale, while it is set up, needs every account to reach
    # (#211). Read once, so the proxy and the resolvers agree.
    allowed = tailnet.allowed()
    # Every managed account gets its block of subordinate ids (rootless
    # containers), and the ruleset filters the block as the account.
    try:
        subids = containers.ensure(policy.effective_users())
    except Exception:  # noqa: BLE001 - never leave the firewall unapplied
        log.exception("could not read subordinate ids")
        subids = {}
    ruleset = nft.render(policy, dns_uid=dnsmasq_uid(), mitm_uid=mitm_uid(),
                         search_uid=search_uid(), build_gid=nixdaemon.build_gid(),
                         channel_search_uid=channel_search_uid(),
                         subids=subids, syncthing=syncthing.nft_inputs())

    # Syntax-check before touching the live ruleset or the boot file.
    with tempfile.NamedTemporaryFile("w", suffix=".nft") as check:
        check.write(ruleset)
        check.flush()
        res = subprocess.run(["nft", "--check", "-f", check.name], capture_output=True, text=True)
        if res.returncode != 0:
            raise ApplyError(f"rendered ruleset failed nft --check: {res.stderr}")

    if _listener_identities_changed(policy):
        stopped = subprocess.run(["systemctl", "stop", MITM_SERVICE],
                                 capture_output=True, text=True)
        if stopped.returncode != 0:
            # Keep both the live firewall and its boot copy unchanged: a
            # new owner must never reach a port with the previous policy.
            raise ApplyError(f"could not stop {MITM_SERVICE} before listener reassignment: {stopped.stderr}")

    _write_atomic(NFT_RULESET_PATH, ruleset, mode=0o600)
    res = subprocess.run(["nft", "-f", str(NFT_RULESET_PATH)], capture_output=True, text=True)
    if res.returncode != 0:
        raise ApplyError(f"nft load failed: {res.stderr}")
    # The load emptied the Syncthing sets; put the running services back,
    # and start or stop each one as the policy now says (syncthing.py).
    try:
        syncthing.apply(policy)
    except Exception:  # noqa: BLE001 - never leave the rest unapplied
        log.exception("could not bring the Syncthing services in line")

    write_mitm_rules(policy, allowed)
    # The proxy runs for a filtered account — and wherever Nix is installed,
    # because the build users' fetches go through it (#123): a stopped proxy
    # would refuse every build, not let it through.
    inspected = (any(u.mode == "filtered" for u in policy.effective_users())
                 or nixdaemon.build_gid() is not None or channel_search_uid() is not None)
    if inspected:
        # Inspection needs the machine to trust the proxy's CA, or every
        # HTTPS page would warn. Generated once, on first use.
        try:
            mitmca.ensure_ca()
        except Exception:  # noqa: BLE001 - never leave the firewall unapplied
            log.exception("could not prepare the inspection CA")
    proxied = inspected or any(u.mode in PROXIED_MODES for u in policy.effective_users())
    lists_now = _lists_fingerprint()
    if proxied:
        restarted = _restart_if_changed(MITM_SERVICE, "\n".join((
            _file_stamp(MITM_RULES_PATH.with_name("listen.env")), lists_now,
            _file_stamp(mitmca.CA_PEM), _connection_policy_stamp())))
        if restarted is not None and restarted.returncode != 0:
            # Do not report a tightened policy as applied while an old
            # passthrough process might still own its accepted streams.
            subprocess.run(["systemctl", "stop", MITM_SERVICE], capture_output=True, text=True)
            raise ApplyError(f"could not restart {MITM_SERVICE}: {restarted.stderr}")
    else:
        subprocess.run(["systemctl", "stop", MITM_SERVICE], capture_output=True, text=True)

    write_nix_users(policy)
    try:
        write_ssh_in(policy)
    except Exception:  # noqa: BLE001 - never leave the rest unapplied
        log.exception("could not tell sshd who may sign in")

    write_search_policy(policy)
    # Search is only useful to somebody whose results need filtering; an
    # unfiltered account has a real search engine already.
    if any(u.mode not in UNFILTERED_MODES and u.mode != "none"
           for u in policy.effective_users()):
        # The engine reads nothing kosherd writes; the front end reads its
        # policy file as it changes, and the lists at start.
        for service in SEARCH_SERVICES:
            _restart_if_changed(service, lists_now if service == SEARCH_FRONT_END else "")
    else:
        for service in reversed(SEARCH_SERVICES):
            subprocess.run(["systemctl", "stop", service],
                           capture_output=True, text=True)

    _write_atomic(DNSMASQ_DROPIN_PATH, dns.render(policy, allowed))
    _write_atomic(SAFESEARCH_PATH, dns.render_safesearch(policy))
    bundle = categories_mod.load_any()
    _write_atomic(CATEGORY_BLOCK_PATH, dns.render_category_blocks(policy, bundle, allowed))
    LEGACY_CATEGORY_BLOCK_PATH.unlink(missing_ok=True)
    # Ads and trackers, for every account: the same drop-in into both
    # resolvers, so the unfiltered account's plain resolver blocks them too.
    adblock = dns.render_adblock(policy, bundle)
    _write_atomic(ADBLOCK_PATH, adblock)
    _write_atomic(ADBLOCK_OPEN_PATH, adblock)

    # The plain resolver exists only for unfiltered users; running it when
    # nobody is unfiltered would just be an unfiltered resolver sitting on
    # the machine.
    unfiltered = any(u.mode in UNFILTERED_MODES for u in policy.effective_users())
    if unfiltered:
        _restart(OPEN_DNS_SERVICE)
    else:
        subprocess.run(["systemctl", "stop", OPEN_DNS_SERVICE],
                       capture_output=True, text=True)
    # The dnsfilter accounts' resolver, likewise only while one exists.
    # Restarted on every change like the family one: the nft load above
    # emptied the dns4/dns6 sets, and only a fresh cache refills them.
    if any(u.mode == "dnsfilter" for u in policy.effective_users()):
        res = _restart(DNSFILTER_DNS_SERVICE)
        if res.returncode != 0:
            log.error("failed to restart %s: %s", DNSFILTER_DNS_SERVICE, res.stderr)
    else:
        subprocess.run(["systemctl", "stop", DNSFILTER_DNS_SERVICE],
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
