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

import base64
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
        self._media: dict[int, str] = {}
        self._youtube: dict[int, dict] = {}

    def rules_for(self, uid: int | None):
        self._refresh()
        return self._rules.get(uid, []) if uid is not None else []

    def blocked_categories_for(self, uid: int | None) -> list:
        self._refresh()
        return self._blocked.get(uid, []) if uid is not None else []

    def media_level_for(self, uid: int | None) -> str:
        self._refresh()
        return self._media.get(uid, "none") if uid is not None else "none"

    def youtube_for(self, uid: int | None) -> dict:
        self._refresh()
        return self._youtube.get(uid, {}) if uid is not None else {}

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
        media: dict[int, str] = {}
        youtube: dict[int, dict] = {}
        for uid_text, raw in doc.items():
            try:
                uid = int(uid_text)
                # Older files were {uid: [rules]}; current ones carry
                # categories too.
                entry = raw if isinstance(raw, dict) else {"rules": raw}
                rules[uid] = parse_rules(entry.get("rules", []))
                blocked[uid] = list(entry.get("blocked_categories", []))
                media[uid] = entry.get("media_level", "none")
                youtube[uid] = dict(entry.get("youtube", {}))
            except Exception as e:  # noqa: BLE001 - one bad entry must not break all
                log.error("bad entry for uid %s: %s", uid_text, e)
        self._rules = rules
        self._blocked = blocked
        self._media = media
        self._youtube = youtube
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




# A 1x1 transparent PNG: replacing an image with this keeps page layout
# intact, which matters — a page whose pictures became broken icons looks
# broken, and people route around things that look broken.
BLANK_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)

IMAGE_TYPES = ("image/",)
# Below this, an image is an icon, a spacer or a tracking pixel: not worth
# hiding and not worth a classifier's time.
MIN_IMAGE_BYTES = 6000


def _is_image(flow) -> bool:
    content_type = (flow.response.headers.get("content-type") or "").lower()
    return content_type.startswith(IMAGE_TYPES)


class YouTube:
    """YouTube limits: restricted mode, categories, and a channel allow-list.

    Restricted Mode is far too coarse on its own for a family that wants
    shiurim but not entertainment, which is why the other two exist.
    """

    WATCH_PATHS = ("/watch", "/shorts", "/embed")

    @staticmethod
    def applies(host: str) -> bool:
        host = (host or "").lower()
        return "youtube.com" in host or "youtube-nocookie.com" in host

    @staticmethod
    def restrict_header(settings: dict) -> str | None:
        level = (settings or {}).get("restrict", "moderate")
        return {"moderate": "Moderate", "strict": "Strict"}.get(level)

    @staticmethod
    def category_of(body: str) -> str | None:
        """The video's category id, read from the watch page."""
        import re

        match = re.search(r'"category"\s*:\s*"([^"]+)"', body)
        if match:
            return match.group(1)
        match = re.search(r'categoryId["\\:\s]+(\d+)', body)
        return match.group(1) if match else None

    @staticmethod
    def channel_of(body: str) -> tuple[str | None, str | None]:
        """(channel id, handle) from the watch page, either of which may be
        what an admin listed."""
        import re

        channel_id = re.search(r'"channelId"\s*:\s*"([^"]+)"', body)
        handle = re.search(r'"canonicalBaseUrl"\s*:\s*"/(@[^"]+)"', body)
        return (channel_id.group(1) if channel_id else None,
                handle.group(1) if handle else None)


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
        flow.metadata["kosher_uid"] = uid

        if YouTube.applies(flow.request.pretty_host):
            header = YouTube.restrict_header(self.policy.youtube_for(uid))
            if header:
                # Per-user rather than blanket: a family may want shiurim
                # unrestricted for a parent and strict for a child.
                flow.request.headers["YouTube-Restrict"] = header

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



    def response(self, flow: http.HTTPFlow) -> None:
        uid = flow.metadata.get("kosher_uid")
        if uid is None:
            return

        if _is_image(flow):
            self._filter_image(flow, uid)
            return

        if YouTube.applies(flow.request.pretty_host):
            self._filter_youtube(flow, uid)

    def _filter_image(self, flow: http.HTTPFlow, uid: int) -> None:
        level = self.policy.media_level_for(uid)
        if level == "none":
            return
        body = flow.response.content or b""
        if len(body) < MIN_IMAGE_BYTES:
            return  # icons, spacers, tracking pixels

        # "all" needs no judgement, which is why it is the only level that is
        # right every time. The others need a classifier and are not wired
        # up yet — see docs/media-filtering.md.
        if level == "all":
            self._blank_image(flow)
            return

        # Source-based suppression costs nothing and covers the worst of the
        # web: if the page's own domain is in a category this user blocks,
        # its imagery goes too.
        host = flow.request.pretty_host or ""
        if self.categories.blocked_categories_of(
                host, self.policy.blocked_categories_for(uid)):
            self._blank_image(flow)

    @staticmethod
    def _blank_image(flow: http.HTTPFlow) -> None:
        flow.response.content = BLANK_PNG
        flow.response.headers["content-type"] = "image/png"
        flow.response.headers["x-kosheros"] = "image-hidden"

    def _filter_youtube(self, flow: http.HTTPFlow, uid: int) -> None:
        settings = self.policy.youtube_for(uid)
        allowed = settings.get("allowed_channels") or []
        blocked = settings.get("blocked_categories") or []
        if not allowed and not blocked:
            return
        path = flow.request.path or ""
        if not any(path.startswith(p) for p in YouTube.WATCH_PATHS):
            return

        content_type = (flow.response.headers.get("content-type") or "").lower()
        if "text/html" not in content_type:
            return
        body = flow.response.get_text(strict=False) or ""

        if allowed:
            channel_id, handle = YouTube.channel_of(body)
            if not ({channel_id, handle} & set(allowed)):
                self._block(flow, flow.request.pretty_url,
                            " because only approved channels are allowed")
                return

        if blocked:
            category = YouTube.category_of(body)
            if category and category in blocked:
                self._block(flow, flow.request.pretty_url,
                            f" because that kind of video is turned off")

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
