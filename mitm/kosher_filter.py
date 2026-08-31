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
    from kosherd import categories as categories_mod
    from kosherd.urlrules import BLOCK, decide, parse_rules
except ImportError:  # pragma: no cover - only when interpreters differ
    # mitmproxy may run under a different interpreter than the one kosherd
    # is installed for. Discover site-packages instead of hardcoding a
    # version: the image moved from Python 3.13 to 3.14 and a pinned path
    # silently rotted.
    import glob

    sys.path.extend(sorted(glob.glob("/usr/lib/python3.*/site-packages")))
    from kosherd import categories as categories_mod
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
  <p>KosherOS blocked <code>{url}</code>{because}.</p>
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
        self._blocked: dict[int, list] = {}

    def rules_for(self, uid: int | None):
        self._refresh()
        return self._rules.get(uid, []) if uid is not None else []

    def blocked_categories_for(self, uid: int | None) -> list:
        self._refresh()
        return self._blocked.get(uid, []) if uid is not None else []

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
        blocked: dict[int, list] = {}
        for uid_text, raw in doc.items():
            try:
                uid = int(uid_text)
                # Older files were {uid: [rules]}; current ones carry
                # categories too.
                entry = raw if isinstance(raw, dict) else {"rules": raw}
                rules[uid] = parse_rules(entry.get("rules", []))
                blocked[uid] = list(entry.get("blocked_categories", []))
            except Exception as e:  # noqa: BLE001 - one bad entry must not break all
                log.error("bad entry for uid %s: %s", uid_text, e)
        self._rules = rules
        self._blocked = blocked
        self._mtime = mtime
        log.info("loaded rules for %d users", len(rules))


# Safe search, enforced on the request itself. DNS already points these
# hostnames at their safe-search addresses for every filtered user; doing it
# here too catches the cases DNS cannot — a query that explicitly asks for
# safe search to be off, and YouTube, which wants a header rather than an
# address.
SAFESEARCH_PARAMS = {
    "google.": {"safe": "active"},
    "bing.": {"adlt": "strict"},
    "duckduckgo.": {"kp": "1"},
    "search.yahoo.": {"vm": "r"},
    "yandex.": {"fyandex": "1"},
}


def _force_safesearch(flow: http.HTTPFlow) -> None:
    host = (flow.request.pretty_host or "").lower()

    for marker, params in SAFESEARCH_PARAMS.items():
        if marker in host:
            query = flow.request.query
            for key, value in params.items():
                query[key] = value
            break

    if "youtube.com" in host or "youtube-nocookie.com" in host:
        # YouTube honours this header on every request, including the app
        # and embedded players.
        flow.request.headers["YouTube-Restrict"] = "Moderate"


class KosherFilter:
    def __init__(self):
        self.uids = UidLookup()
        self.policy = PolicyCache()
        self.categories = categories_mod.load()

    def request(self, flow: http.HTTPFlow) -> None:
        # Everything reaching this proxy belongs to a filtered user: only
        # their traffic is redirected here.
        _force_safesearch(flow)

        client_port = flow.client_conn.peername[1] if flow.client_conn.peername else None
        uid = self.uids.uid_for_port(client_port) if client_port else None
        rules = self.policy.rules_for(uid)

        url = flow.request.pretty_url
        action, pattern = decide(rules, url)
        if action == BLOCK:
            log.info("blocked uid=%s %s (rule: %s)", uid, url, pattern)
            self._block(flow, url, "")
            return

        # An explicit allow rule beats the category lists, so an admin can
        # permit one site from a category they otherwise block.
        if pattern is None:
            hit = self.categories.blocked_categories_of(
                flow.request.pretty_host or "",
                self.policy.blocked_categories_for(uid))
            if hit:
                names = ", ".join(sorted(hit))
                log.info("blocked uid=%s %s (category: %s)", uid, url, names)
                self._block(flow, url, f" because it is {names}")

    def _block(self, flow: http.HTTPFlow, url: str, because: str) -> None:
        flow.response = http.Response.make(
            403,
            BLOCK_PAGE.format(url=_escape(url), because=_escape(because)).encode(),
            {"Content-Type": "text/html; charset=utf-8"},
        )


def _escape(text: str) -> str:
    return (text.replace("&", "&amp;").replace("<", "&lt;")
                .replace(">", "&gt;").replace('"', "&quot;"))[:300]


addons = [KosherFilter()]
