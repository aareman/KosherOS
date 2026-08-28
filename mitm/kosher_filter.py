"""KosherOS mitmproxy addon: per-user URL filtering.

Runs inside mitmproxy in transparent mode. nftables redirects the web
traffic of users in "inspect" mode here (see kosherd/nft.py), so every
request arrives with its real destination recoverable and, after TLS
interception, its full URL visible.

Which user made a request is not carried in the packet, so we look the
client's source port up in /proc/net/tcp{,6} to find the owning uid — the
same trick tools like `ss -p` use. That uid selects the rule set.

The proxy runs unprivileged and never reads the policy itself. kosherd
renders just the URL rules into /var/lib/kosher-mitm/rules.json (uid ->
rules) for it; the file is re-read whenever its mtime changes, so policy
edits take effect without a restart.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from mitmproxy import http

import sys

try:
    from kosherd.urlrules import BLOCK, decide, parse_rules
except ImportError:  # pragma: no cover - only when interpreters differ
    # mitmproxy may run under a different interpreter than the one kosherd
    # is installed for. Discover site-packages instead of hardcoding a
    # version: the image moved from Python 3.13 to 3.14 and a pinned path
    # silently rotted.
    import glob

    sys.path.extend(sorted(glob.glob("/usr/lib/python3.*/site-packages")))
    from kosherd.urlrules import BLOCK, decide, parse_rules

RULES_PATH = Path("/var/lib/kosher-mitm/rules.json")
log = logging.getLogger("kosher-filter")

BLOCK_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>Blocked — KosherOS</title>
<style>
 body {{ font-family: system-ui, sans-serif; background:#0b1a33; color:#e8eefc;
        display:flex; min-height:100vh; align-items:center; justify-content:center;
        margin:0; }}
 .card {{ max-width:32rem; padding:2.5rem; background:#111f3d; border-radius:1rem;
         box-shadow:0 20px 60px rgba(0,0,0,.45); }}
 h1 {{ margin:0 0 .5rem; font-size:1.5rem; }}
 p {{ line-height:1.6; color:#b8c6e4; }}
 code {{ background:#0b1a33; padding:.15rem .4rem; border-radius:.3rem;
        color:#93b4ff; word-break:break-all; }}
</style></head>
<body><div class="card">
  <h1>This page is blocked</h1>
  <p>KosherOS blocked <code>{url}</code>.</p>
  <p>If you need access to this page, ask the administrator of this computer.</p>
</div></body></html>
"""


class UidLookup:
    """Map a local TCP source port to the uid that owns the socket."""

    PATHS = ("/proc/net/tcp", "/proc/net/tcp6")

    def uid_for_port(self, port: int) -> int | None:
        for path in self.PATHS:
            try:
                with open(path) as fh:
                    next(fh)  # header
                    for line in fh:
                        fields = line.split()
                        local = fields[1]
                        if int(local.rsplit(":", 1)[1], 16) == port:
                            return int(fields[7])
            except (OSError, IndexError, ValueError):
                continue
        return None


class PolicyCache:
    """Per-uid rules, reloaded when kosherd rewrites the rules file."""

    def __init__(self, path: Path = RULES_PATH):
        self.path = path
        self._mtime = 0.0
        self._rules: dict[int, list] = {}

    def rules_for(self, uid: int | None):
        self._refresh()
        return self._rules.get(uid, []) if uid is not None else []

    def _refresh(self) -> None:
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            return
        if mtime == self._mtime:
            return
        try:
            doc = json.loads(self.path.read_text())
        except (OSError, ValueError) as e:
            log.error("cannot read rules: %s", e)
            return

        rules: dict[int, list] = {}
        for uid_text, raw in doc.items():
            try:
                rules[int(uid_text)] = parse_rules(raw)
            except Exception as e:  # noqa: BLE001 - one bad rule must not break all
                log.error("bad rules for uid %s: %s", uid_text, e)
        self._rules = rules
        self._mtime = mtime
        log.info("loaded URL rules for %d users", len(rules))


class KosherFilter:
    def __init__(self):
        self.uids = UidLookup()
        self.policy = PolicyCache()

    def request(self, flow: http.HTTPFlow) -> None:
        client_port = flow.client_conn.peername[1] if flow.client_conn.peername else None
        uid = self.uids.uid_for_port(client_port) if client_port else None
        rules = self.policy.rules_for(uid)
        if not rules:
            return

        url = flow.request.pretty_url
        action, pattern = decide(rules, url)
        if action == BLOCK:
            log.info("blocked uid=%s %s (rule: %s)", uid, url, pattern)
            flow.response = http.Response.make(
                403,
                BLOCK_PAGE.format(url=_escape(url)).encode(),
                {"Content-Type": "text/html; charset=utf-8"},
            )


def _escape(text: str) -> str:
    return (text.replace("&", "&amp;").replace("<", "&lt;")
                .replace(">", "&gt;").replace('"', "&quot;"))[:300]


addons = [KosherFilter()]
