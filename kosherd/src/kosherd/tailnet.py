"""Tailscale, driven by kosherd for the administrator (#30).

tailscaled runs as root from the image with no operator, so the only way
to sign it in, read it or sign it out is through the daemon, behind the
same polkit actions and guardian password as every other machine-wide
setting. This module is the part that talks to the `tailscale` command
and keeps the resolvers in step with it; daemon.py holds the D-Bus side.

How the filter survives a tailnet, in one place:

- **Accounts are still filtered.** The output rules (nft.py) match a
  packet by its owner in the output hook, where a filtered account's every
  TCP connection is redirected into its own proxy listener before it is
  routed anywhere. A tailnet address, or the internet through an exit
  node, is just another destination behind those rules: a filtered
  account reaches it through the proxy, a dnsfilter account on the web
  ports only, a whitelist account only by an approved name, and an
  account on "No internet" not at all. tailscaled's own traffic is root's,
  which the rules have always let out.
- **DNS stays ours.** `--accept-dns=false` keeps tailscaled away from
  /etc/resolv.conf. dnsmasq forwards the tailnet's own names to
  tailscaled's resolver instead (dns.render_tailnet), so
  git.example.ts.net resolves for every account while every other name
  goes where it always went.
- **Nothing comes in.** `--shields-up` refuses every connection a tailnet
  peer starts, on top of the firewall's input chain, which drops what is
  not asked for. Tailscale SSH stays off and serve and funnel are never
  used. `--netfilter-mode=off` leaves the firewall to KosherOS's one
  table: tailscaled would otherwise add rules of its own that accept
  anything arriving on tailscale0.
- **Tailscale's own sites and the tailnet's names are allowed (#211)**
  while the computer is signed in or a sign-in is waiting:
  TAILSCALE_HOSTS plus `*.<tailnet>.ts.net` for the one tailnet it is
  signed in to (allowance()). They join the system list for whitelist and
  dnsfilter accounts and are exempt from category blocks in the proxy;
  pages from them are still read like any other page. Signed out, the
  list is empty again.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

from . import dns

log = logging.getLogger(__name__)

TAILSCALE = "/usr/bin/tailscale"

# Every sign-in states the whole configuration. `--reset` makes anything
# not named here go back to its default, so a setting changed by hand on
# a test machine cannot outlive the next sign-in.
UP_FLAGS = (
    "--reset",
    "--accept-dns=false",
    "--ssh=false",
    "--shields-up",
    "--netfilter-mode=off",
    "--accept-routes=false",
)

# How long a sign-in may take to produce the address the administrator
# opens. tailscaled asks the control server for it, so it waits on the
# network, but it comes in a second or two when the network is there.
LOGIN_URL_TIMEOUT = 45
# How long the started sign-in keeps waiting for the administrator to
# finish it in the browser before it gives up.
LOGIN_WAIT = 15 * 60
COMMAND_TIMEOUT = 30

_URL = re.compile(r"https://[^\s\"'<>]+")

# Kept between calls so a second press of "Sign in" replaces the first
# instead of leaving two waiting.
_login_lock = threading.Lock()
_login: subprocess.Popen | None = None


class TailnetError(Exception):
    pass


def installed() -> bool:
    return os.access(TAILSCALE, os.X_OK)


def _run(*args: str, timeout: int = COMMAND_TIMEOUT) -> subprocess.CompletedProcess:
    try:
        return subprocess.run([TAILSCALE, *args], capture_output=True, text=True,
                              timeout=timeout)
    except FileNotFoundError:
        raise TailnetError("Tailscale is not installed on this computer") from None
    except subprocess.TimeoutExpired:
        raise TailnetError("Tailscale did not answer in time") from None


def _message(res: subprocess.CompletedProcess) -> str:
    text = (res.stderr or res.stdout or "").strip()
    return text.splitlines()[-1] if text else f"tailscale exited with {res.returncode}"


def describe(raw: dict) -> dict:
    """What the admin app shows, from `tailscale status --json`.

    Kept to the few things a parent needs: who it is signed in as, which
    tailnet, this computer's name and addresses there, and whether it is
    connected now. The list of the tailnet's other machines is left out.
    """
    state = raw.get("BackendState") or "NoState"
    me = raw.get("Self") or {}
    tailnet = raw.get("CurrentTailnet") or {}
    users = raw.get("User") or {}
    user = users.get(str(me.get("UserID"))) or {}
    signed_in = bool(tailnet) and state not in ("NeedsLogin", "NoState")
    return {
        "installed": True,
        "state": state,
        "signed_in": signed_in,
        "connected": signed_in and state == "Running",
        "user": user.get("LoginName", "") if signed_in else "",
        "tailnet": tailnet.get("Name", "") if signed_in else "",
        "dns_suffix": (tailnet.get("MagicDNSSuffix") or raw.get("MagicDNSSuffix") or "")
        if signed_in else "",
        "name": (me.get("DNSName") or "").rstrip(".") if signed_in else "",
        "addresses": list(me.get("TailscaleIPs") or raw.get("TailscaleIPs") or [])
        if signed_in else [],
        "login_url": raw.get("AuthURL") or "",
        "health": list(raw.get("Health") or []),
    }


def status() -> dict:
    if not installed():
        return {"installed": False, "state": "NotInstalled", "signed_in": False,
                "connected": False}
    res = _run("status", "--json")
    try:
        raw = json.loads(res.stdout)
    except ValueError:
        # tailscaled not running: the command says so on stderr and exits
        # non-zero with nothing to parse.
        return {"installed": True, "state": "Unavailable", "signed_in": False,
                "connected": False, "error": _message(res)}
    return describe(raw)


def login_url_from(lines) -> str | None:
    """The first address `tailscale up` prints for the administrator to open.

    It prints "To authenticate, visit:" and the address on a line of its
    own. Any https address in the output is taken, so a self-hosted
    control server's address works as well as Tailscale's.
    """
    for line in lines:
        found = _URL.search(line)
        if found:
            return found.group(0).rstrip(".,;)")
    return None


def sign_in(on_finished=None) -> str:
    """Start signing this computer in; return the address to open.

    Returns "" when no address was needed (the computer was still signed
    in and came straight back up). The `tailscale up` started here keeps
    waiting for the administrator in the background; when it ends,
    `on_finished(ok)` is called from its thread.
    """
    global _login
    if not installed():
        raise TailnetError("Tailscale is not installed on this computer")
    with _login_lock:
        if _login is not None and _login.poll() is None:
            _login.terminate()
        proc = subprocess.Popen(
            [TAILSCALE, "up", *UP_FLAGS, f"--timeout={LOGIN_WAIT}s"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
        _login = proc

    found: dict = {}
    ready = threading.Event()
    lines: list[str] = []

    def read() -> None:
        for line in proc.stdout:
            lines.append(line.rstrip())
            if "url" not in found:
                url = login_url_from([line])
                if url:
                    found["url"] = url
                    ready.set()
        code = proc.wait()
        found.setdefault("exit", code)
        ready.set()
        if on_finished is not None:
            try:
                on_finished(code == 0)
            except Exception:  # noqa: BLE001 - a callback must not kill the reader
                log.exception("tailscale: after sign-in")

    threading.Thread(target=read, daemon=True, name="tailscale-up").start()
    if not ready.wait(LOGIN_URL_TIMEOUT):
        proc.terminate()
        raise TailnetError("Tailscale did not offer a sign-in page in time. "
                           "Check that this computer is online.")
    if "url" in found:
        return found["url"]
    if found.get("exit") == 0:
        return ""
    raise TailnetError(next((l for l in reversed(lines) if l.strip()),
                            "Tailscale could not start signing in"))


def sign_out() -> None:
    global _login
    with _login_lock:
        if _login is not None and _login.poll() is None:
            _login.terminate()
        _login = None
    res = _run("logout")
    if res.returncode != 0:
        raise TailnetError(_message(res))


# -- what every account may reach while Tailscale is set up (#211) ----------

# Tailscale's own sites, the ones a person opens: the sign-in page and
# the admin console (login.tailscale.com), the downloads
# (pkgs.tailscale.com) and the documentation (tailscale.com). The
# catalogue files tailscale.com under "proxy", which every supervised
# account blocks by default. tailscaled itself is root and was never
# filtered, so the hosts only it talks to (controlplane, the DERP relays,
# log.tailscale.io) are not what this is for; *.tailscale.com covers the
# first two anyway. Nothing broader than tailscale.com.
TAILSCALE_HOSTS = ("tailscale.com", "*.tailscale.com")

_allowed_lock = threading.Lock()
# One status read and its outcome at a time. A read that began before a
# sign-out (the killed `tailscale up`'s own last refresh, or the admin
# app's poll) must not land after it and allow the names again.
_refresh_lock = threading.RLock()
_allowed: tuple[str, ...] = ()
# Set by the daemon: called (from whatever thread noticed) when the list
# changes, to put the new list into enforcement.
on_allowed_change = None


def allowance(now: dict | None, *, waiting: bool = False) -> tuple[str, ...]:
    """The names every account may reach, from a status() reading.

    Tailscale's hosts while the computer is signed in or a sign-in is
    waiting; and, once signed in, `*.<tailnet>` for that one tailnet's
    MagicDNS suffix, never all of ts.net. Nothing otherwise.

    "Waiting" is kosherd's own `tailscale up` still running, and nothing
    else. Not the AuthURL in the status: tailscaled can keep reporting
    the last sign-in address after a sign-out, and the names stayed
    allowed with nobody signing in.
    """
    if not now or not now.get("installed"):
        return ()
    signed_in = bool(now.get("signed_in"))
    if not (signed_in or waiting):
        return ()
    names = list(TAILSCALE_HOSTS)
    suffix = dns.tailnet_name(now.get("dns_suffix")) if signed_in else None
    if suffix:
        names.append("*." + suffix)
    return tuple(names)


def allowed() -> tuple[str, ...]:
    """What allowance() last said; empty until the first status read."""
    return _allowed


def set_allowed(names) -> bool:
    """Record a new list; tell the daemon if it changed. Returns that."""
    global _allowed
    names = tuple(names)
    with _allowed_lock:
        if names == _allowed:
            return False
        _allowed = names
    log.info("tailscale: allowed for every account: %s", ", ".join(names) or "nothing")
    if on_allowed_change is not None:
        try:
            on_allowed_change()
        except Exception:  # noqa: BLE001 - the caller's status read must still answer
            log.exception("tailscale: applying the allowed names")
    return True


def _sign_in_waiting() -> bool:
    with _login_lock:
        return _login is not None and _login.poll() is None


def forget() -> None:
    """After a sign-out: no forward, nothing allowed."""
    with _refresh_lock:
        sync_dns(None)
        set_allowed(())


# -- the resolvers ------------------------------------------------------------

DNS_SERVICE = "kosher-dns.service"
OPEN_DNS_SERVICE = "kosher-dns-open.service"
_dns_lock = threading.Lock()


def sync_dns(suffix: str | None, *, paths=(dns.TAILNET_CONF, dns.TAILNET_OPEN_CONF),
             restart=None) -> bool:
    """Write the tailnet forward into both resolvers; restart what changed.

    Returns whether anything changed. Called after every status read,
    sign-in and sign-out, so the forward follows the tailnet without a
    policy change. A resolver is only restarted when its file changed:
    restarting drops its cache.
    """
    content = dns.render_tailnet(suffix)
    changed = []
    with _dns_lock:
        for path, service in zip(paths, (DNS_SERVICE, OPEN_DNS_SERVICE)):
            p = Path(path)
            try:
                if p.read_text() == content:
                    continue
            except OSError:
                pass
            if not p.parent.is_dir():
                continue
            tmp = p.with_name(p.name + ".tmp")
            tmp.write_text(content)
            os.chmod(tmp, 0o644)
            os.replace(tmp, p)
            changed.append(service)
    for service in changed:
        (restart or _restart)(service)
    return bool(changed)


def _restart(service: str) -> None:
    # try-restart: the plain resolver only runs while somebody is
    # unfiltered, and must not be started just because its file changed.
    if shutil.which("systemctl"):
        subprocess.run(["systemctl", "try-restart", service],
                       capture_output=True, text=True)


def refresh_dns() -> dict | None:
    """Read the status and bring the resolvers, and the names every
    account may reach, in line with it."""
    with _refresh_lock:
        try:
            now = status()
        except TailnetError as e:
            log.info("tailscale: %s", e)
            return None
        if now.get("state") == "Unavailable":
            # tailscaled is starting or stopped; what it last said stands.
            return now
        sync_dns(now.get("dns_suffix") if now.get("signed_in") else None)
        set_allowed(allowance(now, waiting=_sign_in_waiting()))
        return now


def refresh_dns_later(delay: int = 20) -> None:
    """At daemon start, once tailscaled has had a moment to come up."""
    def work():
        time.sleep(delay)
        refresh_dns()

    threading.Thread(target=work, daemon=True, name="tailscale-dns").start()
