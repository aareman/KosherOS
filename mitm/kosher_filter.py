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
import re
from pathlib import Path
from urllib.parse import urlencode, urlsplit

from mitmproxy import http

import sys
import time

try:
    from kosherd import categories as categories_mod
    from kosherd import content as content_mod
    from kosherd import elementfilter as elementfilter_mod
    from kosherd import imageedit as imageedit_mod
    from kosherd import language as language_mod
    from kosherd import accessreq as accessreq_mod
    from kosherd import activity as activity_mod
    from kosherd import search as search_mod
    from kosherd import siterules as siterules_mod
    from kosherd import suggest as suggest_mod
    from kosherd import videocheck as videocheck_mod
    from kosherd import vision as vision_mod
    from kosherd.uidmap import UidLookup
    from kosherd.urlrules import BLOCK, decide, parse_rules
except ImportError:  # pragma: no cover - only when interpreters differ
    # mitmproxy may run under a different interpreter than the one kosherd
    # is installed for. Discover site-packages instead of hardcoding a
    # version: the image moved from Python 3.13 to 3.14 and a pinned path
    # silently rotted.
    import glob

    sys.path.extend(sorted(glob.glob("/usr/lib/python3.*/site-packages")))
    from kosherd import categories as categories_mod
    from kosherd import content as content_mod
    from kosherd import elementfilter as elementfilter_mod
    from kosherd import imageedit as imageedit_mod
    from kosherd import language as language_mod
    from kosherd import accessreq as accessreq_mod
    from kosherd import activity as activity_mod
    from kosherd import search as search_mod
    from kosherd import siterules as siterules_mod
    from kosherd import suggest as suggest_mod
    from kosherd import videocheck as videocheck_mod
    from kosherd import vision as vision_mod
    from kosherd.uidmap import UidLookup
    from kosherd.urlrules import BLOCK, decide, parse_rules

RULES_PATH = Path("/var/lib/kosher-mitm/rules.json")
log = logging.getLogger("kosher-filter")

# The reserved path the block page posts to. It is RELATIVE on purpose:
# posting to a local http:// address from a page the browser considers
# https is mixed content, which browsers refuse. Staying on the blocked
# site's own origin keeps the scheme, and the proxy answers the request
# itself — nothing is ever forwarded to the site.
REQUEST_PATH = "/__kosheros__/request"

BLOCK_STYLE = """
 body { font-family: system-ui, sans-serif; background:#0b1a33; color:#e8eefc;
        display:flex; min-height:100vh; align-items:center; justify-content:center;
        margin:0; }
 .card { max-width:32rem; padding:2.5rem; background:#111f3d; border-radius:1rem;
         box-shadow:0 20px 60px rgba(0,0,0,.45); }
 h1 { margin:0 0 .5rem; font-size:1.5rem; }
 p { line-height:1.6; color:#b8c6e4; }
 code { background:#0b1a33; padding:.15rem .4rem; border-radius:.3rem;
        color:#93b4ff; word-break:break-all; }
 form { margin-top:1.5rem; display:flex; flex-direction:column; gap:.6rem; }
 input, button { font:inherit; padding:.6rem .8rem; border-radius:.5rem;
        border:1px solid #22335c; }
 input { background:#0b1a33; color:#e8eefc; }
 button { background:#7ba2ff; color:#0b1a33; border:0; cursor:pointer;
        font-weight:600; }
"""

BLOCK_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>Blocked — KosherOS</title>
<style>{style}</style></head>
<body><div class="card">
  <h1>This page is blocked</h1>
  <p>KosherOS blocked <code>{url}</code>{because}.</p>
  <p>If you need this page, you can ask the administrator of this
     computer for it. Nothing changes until they say yes.</p>
  <form method="post" action="{request_path}">
    <input type="hidden" name="url" value="{url}">
    <input type="hidden" name="why" value="{why}">
    <input type="text" name="note" maxlength="200"
           placeholder="Why do you need it? (optional)">
    <button type="submit">Ask for this page</button>
  </form>
</div></body></html>
"""

ASKED_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>Asked — KosherOS</title>
<style>{style}</style></head>
<body><div class="card">
  <h1>{heading}</h1>
  <p>{message}</p>
</div></body></html>
"""


# Every connection that reaches this proxy belongs to a FILTERED user:
# nftables redirects only their web traffic here. So a uid we have no rules
# for is never "some other user" — it is a filtered user whose policy has
# not reached us yet (the rules file missing or stale, a just-created
# account, or a source-port lookup that lost a race). Serving that as the
# open web is the failure the first family test hit: an account shows as
# filtered and filters nothing. We fail CLOSED to a safe floor instead.
def _fail_closed_entry() -> dict:
    try:
        blocked = list(categories_mod.DEFAULT_BLOCKED)
    except Exception:  # noqa: BLE001 - categories module unavailable
        blocked = ["adult", "gambling", "dating", "malware", "proxy"]
    return {"rules": [], "blocked_categories": blocked,
            "media_level": "immodest", "cover_style": "frost",
            "language_filter": "substitute",
            "youtube": {"restrict": "strict"}}


class PolicyCache:
    """Per-uid rules, reloaded when kosherd rewrites the rules file.

    Unknown uids do not get an empty policy — they get the fail-closed
    floor, because everything here is a filtered user (see above).
    """

    def __init__(self, path: Path = RULES_PATH):
        self.path = path
        self._mtime = 0.0
        self._known: set[int] = set()
        self._by_port: dict[int, int] = {}
        self._rules: dict[int, list] = {}
        self._blocked: dict[int, list] = {}
        self._media: dict[int, str] = {}
        self._cover: dict[int, str] = {}
        self._language: dict[int, str] = {}
        self._youtube: dict[int, dict] = {}
        self._fallback = _fail_closed_entry()

    def uid_for_listener_port(self, port: int | None) -> int | None:
        """Whose traffic arrives on this port. kosherd gives every filtered
        user their own port and redirects them to it, so this is the whole
        identification — no socket table, nothing to race."""
        self._refresh()
        return self._by_port.get(port) if port is not None else None

    def _is_known(self, uid: int | None) -> bool:
        self._refresh()
        known = uid is not None and uid in self._known
        if not known:
            # Loud, because this is the moment a person is being filtered
            # by the floor instead of their own policy. A run of these in
            # the journal is the signature of a lookup or propagation
            # fault, which is what to go and fix.
            log.warning("no policy for uid=%s; applying the fail-closed floor", uid)
        return known

    def rules_for(self, uid: int | None):
        if not self._is_known(uid):
            return parse_rules(self._fallback["rules"])
        return self._rules.get(uid, [])

    def blocked_categories_for(self, uid: int | None) -> list:
        if not self._is_known(uid):
            return list(self._fallback["blocked_categories"])
        return self._blocked.get(uid, [])

    def media_level_for(self, uid: int | None) -> str:
        if not self._is_known(uid):
            return self._fallback["media_level"]
        return self._media.get(uid, "none")

    def cover_style_for(self, uid: int | None) -> str:
        if not self._is_known(uid):
            return self._fallback["cover_style"]
        return self._cover.get(uid, "frost")

    def language_filter_for(self, uid: int | None) -> str:
        if not self._is_known(uid):
            return self._fallback["language_filter"]
        return self._language.get(uid, "off")

    def youtube_for(self, uid: int | None) -> dict:
        if not self._is_known(uid):
            return dict(self._fallback["youtube"])
        return self._youtube.get(uid, {})

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
        cover: dict[int, str] = {}
        language: dict[int, str] = {}
        youtube: dict[int, dict] = {}
        by_port: dict[int, int] = {}
        for uid_text, raw in doc.items():
            try:
                uid = int(uid_text)
                # Older files were {uid: [rules]}; current ones carry
                # categories too.
                entry = raw if isinstance(raw, dict) else {"rules": raw}
                if entry.get("port") is not None:
                    by_port[int(entry["port"])] = uid
                rules[uid] = parse_rules(entry.get("rules", []))
                blocked[uid] = list(entry.get("blocked_categories", []))
                media[uid] = entry.get("media_level", "none")
                cover[uid] = entry.get("cover_style", "frost")
                language[uid] = entry.get("language_filter", "off")
                youtube[uid] = dict(entry.get("youtube", {}))
            except Exception as e:  # noqa: BLE001 - one bad entry must not break all
                log.error("bad entry for uid %s: %s", uid_text, e)
        self._rules = rules
        self._blocked = blocked
        self._media = media
        self._cover = cover
        self._language = language
        self._youtube = youtube
        self._known = set(rules)
        self._by_port = by_port
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


# Search engines whose result pages are replaced by the local, filtered
# one. Sending a filtered user to Google and then blocking half of what
# they click is the worst of both worlds: they see the explicit snippets
# on the results page anyway, and they learn that the computer is broken.
# KosherOS runs its own search, so use it.
SEARCH_HOSTS = (
    "google.", "bing.com", "duckduckgo.com", "search.yahoo.", "yandex.",
    "search.brave.com", "ecosia.org", "startpage.com", "ask.com",
    "search.marcia", "lite.duckduckgo.com", "html.duckduckgo.com",
)
LOCAL_SEARCH = "http://127.0.0.1:8888/search"
# The query parameter each of them uses.
QUERY_PARAMS = ("q", "p", "text", "query", "wd")


def _search_query(flow: http.HTTPFlow) -> str | None:
    """The search terms, if this request is a search-engine result page."""
    host = (flow.request.pretty_host or "").lower()
    if not any(marker in host for marker in SEARCH_HOSTS):
        return None
    path = flow.request.path.split("?", 1)[0].rstrip("/")
    # "/" and "/search" are result pages; /maps, /images/thumb, api calls
    # and everything else are not, and redirecting those breaks the site.
    if path not in ("", "/search", "/web", "/html", "/lite", "/search.php"):
        return None
    for key in QUERY_PARAMS:
        value = flow.request.query.get(key)
        if value:
            return value
    return None


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
# An inline (data: URI) image in page HTML. Matches the whole URI so a
# substitution swaps the picture and nothing else.
DATA_IMAGE_RE = re.compile(r"data:image/[a-zA-Z0-9.+-]+;base64,[A-Za-z0-9+/=]+")
# What an inline image becomes: a 1x1 transparent PNG, as a data URI.
BLANK_DATA_URI = ("data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAA"
                  "fFcSJAAAADUlEQVR42mNk+P+/HgAFhAJ/wlseKgAAAABJRU5ErkJggg==")

BLANK_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)

IMAGE_TYPES = ("image/",)
# Video is judged by its source first (a blocked category, the mildest
# lever) and then, for an account whose pictures are filtered, by a few of
# its frames: kosherd/videocheck.py samples keyframes from a clip small
# enough to hold and runs them through the picture detector, once per clip.
# Playlists (HLS/DASH manifests) are text that points at segments; the
# segments are the video and are judged one by one.
VIDEO_TYPES = ("video/", "application/vnd.apple.mpegurl",
               "application/x-mpegurl", "application/dash+xml")
PLAYLIST_TYPES = ("application/vnd.apple.mpegurl", "application/x-mpegurl",
                  "application/dash+xml")
# Levels at which a clip is looked at. "all" needs no looking: no video.
VIDEO_CHECKED_LEVELS = ("nsfw", "suggestive", "immodest", "people")
# Levels at which an UNCHECKABLE clip (no decoder, too big to hold, a
# mid-stream range with no verdict yet) is refused rather than passed. The
# mildest level keeps its old behaviour — video passed unchecked there
# before this existed, and a decoder that is missing must not take it away.
VIDEO_STRICT_LEVELS = ("all", "people", "immodest", "suggestive")
# Kept for callers that still read it: what "no open-web video" meant.
VIDEO_BLOCKED_LEVELS = VIDEO_STRICT_LEVELS

# Responses the filter never reads, so they need not be held in memory:
# they go straight through as they arrive. Everything else was buffered
# whole before the filter ran — including a 200 MB download and every
# video, at every media level — which is the memory and the stall a person
# felt as "the system is slow". Only the types the filter actually inspects
# (HTML, pictures, clips it will sample, the JSON of a few known sites) are
# held now.
STREAM_TYPES = ("application/octet-stream", "application/zip", "application/gzip",
                "application/x-tar", "application/x-7z-compressed",
                "application/x-xz", "application/x-bzip2", "application/pdf",
                "application/wasm", "application/vnd.debian.binary-package",
                "application/x-rpm", "application/x-iso9660-image",
                "font/", "audio/")
# Anything this large that is not a page, a picture or a clip is a download.
STREAM_LARGE_BYTES = 8 * 1024 * 1024
# Below this, an image is an icon, a spacer or a tracking pixel: not worth
# hiding and not worth a classifier's time.
MIN_IMAGE_BYTES = 2500  # keep in step with kosherd.vision
# Media level -> catalogue categories whose sites get their imagery hidden
# outright at that level. The weakest level only distrusts adult sites; the
# modesty levels also distrust celebrity/immodest ones.
IMMODEST_SOURCES = {
    "nsfw": frozenset({"adult"}),
    "suggestive": frozenset({"adult", "immodest"}),
    "immodest": frozenset({"adult", "immodest"}),
    "people": frozenset({"adult", "immodest"}),
}


# The thumbnail hosts of the big image searches. Not page images — these
# serve nothing but shrunken copies of arbitrary web imagery.
def _is_search_thumb(host: str) -> bool:
    """Aggregated small pictures of the whole web, too small to judge
    reliably and from a host the family cannot curate: image search, and
    YouTube's video thumbnails, which at the modesty levels are the worst
    of what a child sees on the site."""
    host = host.lower()
    return (host.endswith("mm.bing.net")
            or host == "i.ytimg.com" or host.endswith(".ytimg.com")
            or (host.startswith("encrypted-tbn") and host.endswith(".gstatic.com"))
            or host == "external-content.duckduckgo.com"
            or host.endswith(".qwant.com") and "pics" in host)


# Pictures and clips that arrive without saying what they are: CDNs serve
# .mp4, .webm and .gif as application/octet-stream, and a filter keyed on
# the content type alone streamed them through unchecked — "gif/videos
# getting through and not checked". The address's extension says then.
OCTET_TYPES = ("application/octet-stream", "binary/octet-stream")
VIDEO_EXTENSIONS = (".mp4", ".m4v", ".webm", ".mov", ".m4s", ".ts", ".mkv",
                    ".ogv", ".3gp", ".gifv")
IMAGE_EXTENSIONS = (".gif", ".png", ".jpg", ".jpeg", ".webp", ".avif", ".bmp")


def _path_extension(flow) -> str:
    try:
        path = urlsplit(flow.request.pretty_url).path.lower()
    except Exception:  # noqa: BLE001
        return ""
    dot = path.rfind(".")
    return path[dot:] if dot >= 0 and "/" not in path[dot:] else ""


def _is_image(flow) -> bool:
    content_type = (flow.response.headers.get("content-type") or "").lower()
    if content_type.startswith(IMAGE_TYPES):
        return True
    return content_type.startswith(OCTET_TYPES) and _path_extension(flow) in IMAGE_EXTENSIONS


def _is_video(flow) -> bool:
    content_type = (flow.response.headers.get("content-type") or "").lower()
    if content_type.startswith(VIDEO_TYPES):
        return True
    return content_type.startswith(OCTET_TYPES) and _path_extension(flow) in VIDEO_EXTENSIONS


def _is_playlist(flow) -> bool:
    content_type = (flow.response.headers.get("content-type") or "").lower()
    return content_type.startswith(PLAYLIST_TYPES)


def _content_length(flow) -> int | None:
    try:
        return int(flow.response.headers.get("content-length", ""))
    except (TypeError, ValueError):
        return None


def _range_info(flow):
    """(first byte, total size) of a partial response, from Content-Range;
    (0, content-length) for a whole one. None where unknown."""
    header = flow.response.headers.get("content-range") or ""
    m = re.match(r"bytes (\d+)-(\d+)/(\d+|\*)", header)
    if m:
        total = int(m.group(3)) if m.group(3) != "*" else None
        return int(m.group(1)), total
    return 0, _content_length(flow)


class YouTube:
    """YouTube limits: restricted mode, categories, and a channel allow-list.

    Restricted Mode is far too coarse on its own for a family that wants
    shiurim but not entertainment, which is why the other two exist.
    """

    WATCH_PATHS = ("/watch", "/shorts", "/embed", "/live", "/v/")

    # YouTube is a single-page app. After the first load, every video is
    # fetched as JSON from these and no watch page is ever parsed again —
    # so a filter that only reads the HTML checks the first video a child
    # opens and nothing they click afterwards. This is where the real
    # enforcement has to happen.
    PLAYER_PATHS = ("/youtubei/v1/player", "/youtubei/v1/reel/reel_item_watch")
    # The endpoints that only Shorts use: the reel player and the endless
    # sequence behind it. Refused outright when Shorts are turned off.
    REEL_PATHS = ("/youtubei/v1/reel/",)
    SHORTS_KIND = "shorts"
    # How a feed entry says it is a short, by shape: a Shorts shelf, a reel
    # item, the newer lockup, or any entry whose link is a reel link.
    SHORTS_KEYS = frozenset({"reelShelfRenderer", "reelItemRenderer",
                             "shortsLockupViewModel", "reelWatchEndpoint"})

    # Hovering a thumbnail plays the video, quietly and without ever asking
    # the player whether it may: the feed carries an inline player and a
    # moving thumbnail, and both start on hover — "youtube thumbnail
    # previews play on hover even for blocked videos". These keys are taken
    # out of the feed, so a thumbnail is a picture again.
    PREVIEW_KEYS = frozenset({
        "inlinePlaybackRenderer", "movingThumbnailRenderer",
        "movingThumbnailDetails", "animatedThumbnailRenderer",
        "richGridMovingThumbnailRenderer", "onHoverInlinePlaybackRenderer",
    })
    # And the pictures those previews are: an animated WebP of the video
    # itself, served from the thumbnail host. A backstop for a client that
    # asks for one anyway.
    PREVIEW_PATHS = ("/an_webp/", "/an_/")

    # The feeds: the home page, search results, the sidebar of suggestions.
    # Only pruned for an account limited to approved channels, and only
    # then because the alternative is a wall of videos that all fail to
    # play — which teaches a child that the computer is broken rather than
    # that the choice was made deliberately.
    FEED_PATHS = ("/youtubei/v1/browse", "/youtubei/v1/search",
                  "/youtubei/v1/next", "/youtubei/v1/guide")

    # Where a renderer names the channel it belongs to. Matched by shape
    # rather than by searching the text: a title that happens to mention
    # an approved channel is not that channel's video.
    CHANNEL_KEYS = ("ownerText", "longBylineText", "shortBylineText")

    # The API hosts as well as the site. YouTube's clients call the same
    # /youtubei/v1/* endpoints on googleapis.com — the resolver knows it and
    # points them at Restricted Mode — and a request that went there was
    # answered with no category, channel or Shorts rule applied at all,
    # because this list was the site only. Exact hosts, never
    # "googleapis.com": that would be every Google API.
    HOSTS = ("youtube.com", "youtube-nocookie.com", "youtubekids.com",
             "youtubei.googleapis.com", "youtube.googleapis.com")

    @staticmethod
    def applies(host: str) -> bool:
        host = (host or "").lower().strip(".")
        # Suffix match, not "in": youtube.com.attacker.example is not
        # YouTube, and treating it as such would apply the wrong rules to
        # somebody else's site.
        return any(host == h or host.endswith("." + h) for h in YouTube.HOSTS)

    @staticmethod
    def is_player_api(path: str) -> bool:
        path = (path or "").split("?", 1)[0]
        return any(path.startswith(p) for p in YouTube.PLAYER_PATHS)

    @staticmethod
    def is_reel_api(path: str) -> bool:
        path = (path or "").split("?", 1)[0]
        return any(path.startswith(p) for p in YouTube.REEL_PATHS)

    @staticmethod
    def is_shorts_page(path: str) -> bool:
        return (path or "").split("?", 1)[0].startswith("/shorts")

    @staticmethod
    def playing_a_short(flow) -> bool:
        """Is this player request for a short? The web app plays shorts
        through the same /player call as everything else; what tells them
        apart is the page the app is on, which same-origin requests carry
        in their referer."""
        path = flow.request.path or ""
        if YouTube.is_reel_api(path):
            return True
        referer = (flow.request.headers.get("referer") or "")
        return "/shorts/" in referer or referer.rstrip("/").endswith("/shorts")

    @staticmethod
    def mentions_shorts(node, depth: int = 0) -> bool:
        """Does this feed entry carry a Shorts shape anywhere in it?"""
        if depth > 12:
            return False
        if isinstance(node, dict):
            if node.keys() & YouTube.SHORTS_KEYS:
                return True
            return any(YouTube.mentions_shorts(v, depth + 1) for v in node.values())
        if isinstance(node, list):
            return any(YouTube.mentions_shorts(v, depth + 1) for v in node)
        return False

    @staticmethod
    def is_moving_thumbnail(host: str, path: str) -> bool:
        """An animated preview of a video, not a thumbnail of one."""
        if not _is_search_thumb(host or ""):
            return False
        path = (path or "").split("?", 1)[0]
        return (any(part in path for part in YouTube.PREVIEW_PATHS)
                or "_6s." in path)

    @staticmethod
    def prune_previews(node, depth: int = 0):
        """Take the hover players out of a feed.

        Keys, not list entries: the video stays in the feed as a picture
        and a title, and only the part that plays it on hover goes. A feed
        whose entries were dropped instead would hide videos the account
        may perfectly well watch.
        """
        if depth > 24:
            return node
        if isinstance(node, dict):
            return {k: YouTube.prune_previews(v, depth + 1)
                    for k, v in node.items() if k not in YouTube.PREVIEW_KEYS}
        if isinstance(node, list):
            return [YouTube.prune_previews(item, depth + 1) for item in node]
        return node

    @staticmethod
    def prune_shorts(node, depth: int = 0):
        """Take the Shorts out of a feed: shelves, reel items and lockups.
        Only list entries are dropped, never a dict's keys, so the app's
        structure stays what it expects."""
        if depth > 24:
            return node
        if isinstance(node, list):
            return [YouTube.prune_shorts(item, depth + 1) for item in node
                    if not (isinstance(item, dict) and YouTube.mentions_shorts(item))]
        if isinstance(node, dict):
            return {k: YouTube.prune_shorts(v, depth + 1) for k, v in node.items()}
        return node

    @staticmethod
    def is_feed_api(path: str) -> bool:
        path = (path or "").split("?", 1)[0]
        return any(path.startswith(p) for p in YouTube.FEED_PATHS)

    @staticmethod
    def renderer_channel(item: dict) -> set:
        """Every way this item names its channel: id, handle and title."""
        found = set()
        for key in YouTube.CHANNEL_KEYS:
            runs = (item.get(key) or {}).get("runs") or []
            for run in runs:
                if isinstance(run, dict):
                    text = run.get("text")
                    if text:
                        found.add(text)
                    endpoint = ((run.get("navigationEndpoint") or {})
                                .get("browseEndpoint") or {})
                    if endpoint.get("browseId"):
                        found.add(endpoint["browseId"])
                    url = endpoint.get("canonicalBaseUrl") or ""
                    if url.startswith("/@"):
                        found.add(url[1:])
        if item.get("channelId"):
            found.add(item["channelId"])
        return found

    @staticmethod
    def prune_feed(node, allowed: set, depth: int = 0):
        """Drop videos from a feed that this account could not play anyway.

        Conservative on purpose: only entries that BOTH look like a video
        renderer AND name a channel are considered, so anything whose shape
        is unfamiliar is left exactly as it was. A filter that guesses at
        an app's internals breaks it.
        """
        if depth > 24:
            return node
        if isinstance(node, list):
            kept = []
            for item in node:
                if isinstance(item, dict):
                    dropped = False
                    for value in item.values():
                        if not isinstance(value, dict):
                            continue
                        named = YouTube.renderer_channel(value)
                        if named and not (named & allowed):
                            dropped = True
                            break
                    if dropped:
                        continue
                kept.append(YouTube.prune_feed(item, allowed, depth + 1))
            return kept
        if isinstance(node, dict):
            return {k: YouTube.prune_feed(v, allowed, depth + 1)
                    for k, v in node.items()}
        return node

    @staticmethod
    def restrict_header(settings: dict) -> str | None:
        level = (settings or {}).get("restrict", "moderate")
        return {"moderate": "Moderate", "strict": "Strict"}.get(level)

    # What the app shows when a video genuinely cannot be played. Reusing
    # YouTube's own shape means the page renders a normal message instead
    # of spinning forever or showing a broken player.
    #
    # The first line names KosherOS. A person who opens a video by its
    # address gets our block page and knows who stopped it; a person who
    # clicks one inside the app gets this, in YouTube's own frame, and
    # used to see only "turned off on this computer" — the same decision
    # wearing two faces, which reads as the filter being inconsistent. The
    # words below are the block page's words.
    BLOCKED_TITLE = "Blocked by KosherOS"
    ASK_LINE = ("You can ask the administrator of this computer for it. "
                "Nothing changes until they say yes.")

    @staticmethod
    def unplayable(reason: str) -> dict:
        detail = reason.rstrip(".") + ". " + YouTube.ASK_LINE
        return {
            "playabilityStatus": {
                "status": "ERROR",
                "reason": YouTube.BLOCKED_TITLE,
                "errorScreen": {"playerErrorMessageRenderer": {
                    "reason": {"simpleText": YouTube.BLOCKED_TITLE},
                    "subreason": {"simpleText": detail},
                }},
            },
            "videoDetails": {},
        }

    @staticmethod
    def category_of(body: str) -> str | None:
        """The video's category id, from the player JSON or the watch page.

        YouTube says "Music", the settings say "10": the name is mapped to
        its id, and an id is taken as it is. The microformat's category is
        preferred over any other "category" key in the body, since that is
        the one that describes the video; the first match used to be taken,
        and it was not always that one.
        """
        import re

        from kosherd.policy import youtube_category_id

        # The renderer's own category sits after its thumbnails, embed and
        # description — nine thousand characters in, on a real response —
        # so the window is generous; non-greedy, it stops at the first one.
        for pattern in (r'"playerMicroformatRenderer"[\s\S]{0,60000}?"category"\s*:\s*"([^"]+)"',
                        r'"category"\s*:\s*"([^"]+)"'):
            match = re.search(pattern, body)
            if match:
                found = youtube_category_id(match.group(1))
                if found:
                    return found
        match = re.search(r'categoryId["\\:\s]+(\d+)', body)
        return youtube_category_id(match.group(1)) if match else None

    @staticmethod
    def channel_of(body: str) -> tuple[str | None, str | None]:
        """(channel id, handle) from the watch page, either of which may be
        what an admin listed."""
        import re

        channel_id = re.search(r'"channelId"\s*:\s*"([^"]+)"', body)
        handle = re.search(r'"canonicalBaseUrl"\s*:\s*"/(@[^"]+)"', body)
        return (channel_id.group(1) if channel_id else None,
                handle.group(1) if handle else None)


# How much a page may say before it is blocked, per media level — the same
# ladder the search results use, so a page reached by clicking a link and a
# page reached from search are judged alike.
CONTENT_TOLERANCE = {
    "none": content_mod.NSFW,
    "nsfw": content_mod.NSFW,
    "suggestive": content_mod.SUGGESTIVE,
    "immodest": content_mod.IMMODEST,
    "people": content_mod.IMMODEST,
    "all": content_mod.IMMODEST,
}
# Reading a whole page costs real time on a slow machine, and the character
# of a page is in its head and its first screens.
MAX_SCORED_BYTES = 200_000


class KosherFilter:
    def __init__(self):
        self.uids = UidLookup()
        self.policy = PolicyCache()
        self.categories = categories_mod.load_any()
        self.scorer = content_mod.load()
        self.wordlist = language_mod.load()
        self.vision = vision_mod.ImageFilter()
        self.videos = videocheck_mod.VideoChecker(self.vision)
        # Recently covered pictures by content hash, so a reload or the
        # same photo on the next page is not frosted and re-encoded again.
        self.covers = imageedit_mod.CoverCache()
        self.siterules = siterules_mod.load()
        # What each page was judged to be, so the pictures ON it can be
        # judged in that light. Small and bounded: a browser fetches a
        # page's images within seconds of the page.
        self.page_levels: dict[str, str] = {}
        self.blocklist = search_mod.load_blocklist()

    def _uid_of(self, flow) -> int | None:
        """Who made this connection: the LISTENER PORT it arrived on.

        nftables sends each filtered user to their own transparent listener
        (kosherd/nft.py mitm_ports), so the port that accepted the
        connection is the person's identity — a map lookup, nothing to
        race. The port comes from the flow's proxy_mode, NOT from
        client_conn.sockname: in transparent mode mitmproxy overwrites
        sockname with the ORIGINAL DESTINATION (the real server's IP:443),
        so reading a listener port there always missed and fell through to
        the socket-table scan, which under a browser's connection churn
        returns the wrong owner or none — the exact non-determinism the
        per-user ports exist to remove.
        """
        port = self._listener_port(flow)
        uid = self.policy.uid_for_listener_port(port)
        if uid is not None:
            return uid
        # Fallback for a rules file that predates per-user ports, or a mode
        # we could not read a port from. Matches the full 4-tuple on live
        # sockets; still racy, hence the fallback and not the primary path.
        peer = getattr(flow.client_conn, "peername", None)
        if not peer:
            return None
        dest = getattr(getattr(flow, "server_conn", None), "address", None) or (None, None)
        return self.uids.uid_for_connection(peer[0], peer[1], dest[0], dest[1])

    @staticmethod
    def _listener_port(flow) -> int | None:
        """The local port this connection was accepted on, from the flow's
        proxy mode. `custom_listen_port` is the direct answer; the spec
        string ('transparent@127.0.0.1:30000') is the fallback if a future
        mitmproxy drops the attribute."""
        mode = getattr(flow.client_conn, "proxy_mode", None)
        if mode is None:
            return None
        port = getattr(mode, "custom_listen_port", None)
        if isinstance(port, int):
            return port
        spec = getattr(mode, "full_spec", None) or str(mode)
        match = re.search(r"@[^@]*:(\d+)\b", spec)
        return int(match.group(1)) if match else None

    def request(self, flow: http.HTTPFlow) -> None:
        # Everything reaching this proxy belongs to a filtered user: only
        # their traffic is redirected here.
        _force_safesearch(flow)

        uid = self._uid_of(flow)
        flow.metadata["kosher_uid"] = uid

        # Reserved on every host, because the form is posted to the
        # blocked site's own origin (see REQUEST_PATH). The site never
        # sees it.
        if flow.request.method == "POST" and \
                flow.request.path.split("?", 1)[0] == REQUEST_PATH:
            self._handle_request_form(flow, uid)
            return

        query = _search_query(flow)
        if query is not None:
            params = urlencode({"q": query})
            flow.response = http.Response.make(
                302, b"", {"Location": f"{LOCAL_SEARCH}?{params}",
                           "Cache-Control": "no-store"})
            return

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
            self._block(flow, url, "", why=f"rule:{pattern}")
            return

        # An explicit allow rule beats the category lists, so an admin can
        # permit one site from a category they otherwise block.
        if pattern is None:
            blocked = self.policy.blocked_categories_for(uid)
            hit = self.categories.blocked_categories_of(
                flow.request.pretty_host or "", blocked)
            if hit:
                names = ", ".join(sorted(hit))
                log.info("blocked uid=%s %s (category: %s)", uid, url, names)
                self._block(flow, url, f" because it is {names}",
                            why="category:" + ",".join(sorted(hit)))
                return

            # A department on a shop the family uses. Read from the
            # address, so it costs microseconds and happens before the
            # page is fetched.
            if siterules_mod.GATING_CATEGORY in blocked:
                why = self.siterules.reason(url) or self._blocked_shop_search(url)
                if why:
                    log.info("blocked uid=%s %s (%s)", uid, url, why)
                    self._block(flow, url, f" because it is {why}",
                                why=f"shop:{why}")



    # ---- what to hold and what to let through as it arrives ----------------

    def responseheaders(self, flow: http.HTTPFlow) -> None:
        """Decide, from the headers alone, whether the body must be held.

        mitmproxy buffers every response body before `response` runs unless
        told otherwise here. The filter only ever reads pages, pictures and
        the clips it samples; holding downloads and streams as well cost the
        memory and the stall that made the machine feel slow. A clip that
        must be refused is refused HERE, before a byte of it is fetched.
        """
        uid = flow.metadata.get("kosher_uid")
        if uid is None:
            # Not a filtered account's connection (see response): nothing
            # to inspect, so nothing to hold.
            flow.response.stream = True
            return
        content_type = (flow.response.headers.get("content-type") or "").lower()
        if content_type.startswith(PLAYLIST_TYPES):
            return  # small text; response() decides
        if _is_video(flow):
            decision = self._video_decision(flow, uid)
            if decision == "stream":
                flow.response.stream = True
            elif decision == "refuse":
                self._refuse_video(flow, before_body=True)
            return  # "hold": sampled in response()
        if _is_image(flow):
            if YouTube.is_moving_thumbnail(flow.request.pretty_host or "",
                                           getattr(flow.request, "path", "") or ""):
                return  # response() decides: it may be a video in a picture
            level = self.policy.media_level_for(uid)
            if level == "none" or self._recently_clean(flow, level):
                flow.response.stream = True
            return
        if "text/html" in content_type or YouTube.applies(flow.request.pretty_host) \
                or self.siterules.is_suggestions(flow.request.pretty_url):
            return
        length = _content_length(flow)
        if content_type.startswith(STREAM_TYPES) or (
                length is not None and length > STREAM_LARGE_BYTES
                and not content_type.startswith("text/")):
            flow.response.stream = True

    async def response(self, flow: http.HTTPFlow) -> None:
        # Async so that a picture being judged, or a clip being sampled,
        # waits on its own: the synchronous hook held the proxy's one event
        # loop for up to the detection deadline per picture, and every other
        # connection on the machine stood still with it.
        uid = flow.metadata.get("kosher_uid")
        if uid is None:
            return
        if getattr(flow.response, "stream", False):
            return  # let through as it arrived; there is no body to read

        if _is_image(flow):
            if self._youtube_preview_refused(flow, uid):
                return
            await self._filter_image_async(flow, uid)
            return

        if _is_video(flow):
            await self._filter_video_async(flow, uid)
            return

        if YouTube.applies(flow.request.pretty_host):
            self._filter_youtube(flow, uid)
            return

        if self.siterules.is_suggestions(flow.request.pretty_url):
            self._filter_suggestions(flow, uid)
            return

        self._filter_page(flow, uid)

    def _youtube_limited(self, uid: int) -> bool:
        """Whether this account has YouTube limits of any kind."""
        settings = self.policy.youtube_for(uid)
        return bool(settings.get("allowed_channels") or settings.get("blocked_categories"))

    def _youtube_preview_refused(self, flow: http.HTTPFlow, uid: int) -> bool:
        """Hide an animated preview of a video for an account that limits
        YouTube. True when the response has been answered."""
        if not YouTube.is_moving_thumbnail(flow.request.pretty_host or "",
                                           getattr(flow.request, "path", "") or ""):
            return False
        if not self._youtube_limited(uid):
            return False
        log.info("hid a YouTube hover preview for uid=%s", uid)
        self._blank_image(flow)
        return True

    def _blocked_shop_search(self, url: str) -> str | None:
        """A search typed into a shop's own box.

        The department rules cover navigating TO a department; the search
        box on the same page walks straight past them.
        """
        why = self.siterules.blocked_search(url)
        if why:
            return why
        text = self.siterules.search_text(url)
        if text and self.blocklist.contains_any(text):
            return "a blocked search"
        return None

    def _is_blocked_suggestion(self, uid: int):
        """A test for one autocomplete entry, or None if none is needed."""
        blocked = self.policy.blocked_categories_for(uid)
        if siterules_mod.GATING_CATEGORY not in blocked:
            return None
        tolerance = self._content_tolerance(uid)

        def is_blocked(text: str) -> bool:
            if self.blocklist.contains_any(text):
                return True
            # A suggestion is a few words, not a page: the scorer needs
            # more evidence than one exists, so the department term list
            # is what answers here.
            if self.siterules.blocked_term(text):
                return True
            return self.scorer.score(text).at_least(tolerance)

        return is_blocked

    def _filter_suggestions(self, flow: http.HTTPFlow, uid: int) -> None:
        is_blocked = self._is_blocked_suggestion(uid)
        if is_blocked is None:
            return
        filtered = suggest_mod.filter_json(flow.response.content or b"",
                                           is_blocked)
        if filtered is None:
            return
        flow.response.content = filtered
        flow.response.headers["x-kosheros"] = "suggestions-filtered"
        # The body changed, so any length or integrity header on it is now
        # a lie the browser would enforce.
        flow.response.headers.pop("content-length", None)

    def _strip_shop_navigation(self, flow, host: str, body: str):
        """Take the offending items out of a covered page.

        A shop names its whole catalogue in the navigation of every page.
        Scoring that gives two bad answers and no good one: strictly, and
        Amazon is blocked outright; loosely, and the sidebar stays on
        screen. Removing the items is neither — the words are gone and
        what is left is a page about socks, which can then be judged as
        strictly as anything else.
        """
        spec = self.siterules.strip_spec(host)
        if spec is None:
            return body, False
        tags, is_blocked, quick = spec
        stripped, removed = elementfilter_mod.strip(
            body, is_blocked, tags=tags, quick_reject=quick)
        if removed:
            log.info("removed %d navigation item(s) from %s", removed, host)
            flow.response.text = stripped
            flow.response.headers["x-kosheros"] = f"items-removed={removed}"
        # True means the page was small enough to rewrite, so what is left
        # can be judged strictly — not that anything was actually removed.
        return stripped, len(body) <= elementfilter_mod.MAX_PAGE

    def _content_tolerance(self, uid: int, host: str = "",
                           stripped: bool = False) -> str:
        """How much a page may say before it is blocked for this user.

        Driven by the media level, but never weaker than what the blocked
        categories already imply: an account that blocks immodest sites did
        not ask to read an immodest page on a site it does not block.

        On a shop with rules of its own, the floor stops at "suggestive".
        Those sites carry their whole department list in the navigation of
        every page — an Amazon search for socks names lingerie, bras,
        panties and swimwear in the sidebar and scores immodest on that
        alone. Blocking it would be an outage, not a filter. The precise
        rules (department addresses, the search box, autocomplete) are what
        handle immodest there; the scorer is only the backstop for worse.
        """
        level = self.policy.media_level_for(uid)
        tolerance = CONTENT_TOLERANCE.get(level, content_mod.NSFW)
        if siterules_mod.GATING_CATEGORY in self.policy.blocked_categories_for(uid):
            floor = content_mod.IMMODEST
            if host and self.siterules.covers(host) and not stripped:
                # The navigation could not be removed (too large a page, a
                # parse that failed), so its catalogue vocabulary is still
                # in the text and would convict a page about socks.
                floor = content_mod.SUGGESTIVE
            if content_mod.SEVERITY[tolerance] > content_mod.SEVERITY[floor]:
                tolerance = floor
        return tolerance

    def _filter_page(self, flow: http.HTTPFlow, uid: int) -> None:
        """Judge a page by its words when no list has anything to say about it.

        Domain lists are the backbone of the filter, but they only know
        sites somebody has already catalogued. A page on a host nothing has
        classified is exactly what gets through, and here — unlike at the
        DNS layer — we can simply read it.
        """
        level = self.policy.media_level_for(uid)
        language_filter = self.policy.language_filter_for(uid)
        immodest_blocked = siterules_mod.GATING_CATEGORY in \
            self.policy.blocked_categories_for(uid)
        if level == "none" and language_filter == "off" and not immodest_blocked:
            return
        content_type = (flow.response.headers.get("content-type") or "").lower()
        if "text/html" not in content_type:
            return
        body = flow.response.get_text(strict=False) or ""
        if not body:
            return
        stripped = False
        if level == "all" and "data:image/" in body:
            # Shopping sites inline thumbnails as data: URIs straight into
            # the HTML, so they never appear as image responses at all —
            # and "block all pictures" showed pictures. Replace every
            # inline image with the blank, and write the page back.
            body = DATA_IMAGE_RE.sub(BLANK_DATA_URI, body)
            flow.response.text = body
            stripped = True
        if immodest_blocked:
            body, nav_stripped = self._strip_shop_navigation(
                flow, flow.request.pretty_host or "", body)
            stripped = stripped or nav_stripped
        text = content_mod.visible_text(body[:MAX_SCORED_BYTES])

        if language_filter != "off" and self.wordlist.contains_any(text):
            if language_filter == "block":
                self._block(flow, flow.request.pretty_url,
                            " because of the language on it", why="language")
                return
            cleaned, count = language_mod.clean_html(body, self.wordlist)
            if count:
                flow.response.text = cleaned
                flow.response.headers["x-kosheros"] = f"language-cleaned={count}"
            # The page has been rewritten; score the cleaned copy below.
            text = content_mod.visible_text(cleaned[:MAX_SCORED_BYTES])

        if level == "none" and not immodest_blocked:
            return
        # An explicit ALLOW rule is the administrator overriding the
        # machine's judgement for this page; the content scorer must honour
        # it. It did not: rules were only consulted in the request hook, so
        # allowing a wrongly-blocked site changed nothing — the response
        # scorer blocked it again a moment later.
        action, pattern = decide(self.policy.rules_for(uid),
                                 flow.request.pretty_url)
        if action != BLOCK and pattern is not None:
            return
        tolerance = self._content_tolerance(
            uid, flow.request.pretty_host or "", stripped)
        verdict = self.scorer.score(text)
        self._remember_page(flow.request.pretty_url, verdict.level)
        if verdict.at_least(tolerance):
            log.info("blocked uid=%s %s (content: %s, %d points, %s)",
                     uid, flow.request.pretty_url, verdict.level,
                     verdict.points, ", ".join(verdict.hits))
            self._block(flow, flow.request.pretty_url,
                        f" because the page reads as {verdict.level}",
                        why=f"content:{verdict.level}")

    MAX_REMEMBERED_PAGES = 64

    def _remember_page(self, url: str, level: str) -> None:
        if level == content_mod.CLEAN:
            self.page_levels.pop(url, None)
            return
        self.page_levels[url] = level
        while len(self.page_levels) > self.MAX_REMEMBERED_PAGES:
            self.page_levels.pop(next(iter(self.page_levels)))

    def _referring_page_level(self, flow: http.HTTPFlow) -> str:
        """What the page this picture is on was judged to be."""
        referer = flow.request.headers.get("referer") or ""
        return self.page_levels.get(referer, "")

    # ---- pictures ----------------------------------------------------------
    #
    # The picture path is three steps so the same logic serves the proxy
    # (async, off the event loop) and the tests (sync): the free decisions
    # before the model, the decision after its verdict, and the cover.

    def _image_prejudge(self, flow: http.HTTPFlow, uid: int, level: str) -> bool:
        """The decisions that need no model. True when the response is settled."""
        body = flow.response.content or b""

        # "all" needs no judgement, which is why it is the only level that
        # is right every time — and it comes BEFORE the small-image gate:
        # "block all pictures" must mean all of them, and shopping sites'
        # thumbnails fit comfortably under any byte floor.
        if level == "all":
            self._blank_image(flow)
            return True

        if len(body) < MIN_IMAGE_BYTES:
            return True  # icons, spacers, tracking pixels

        # Source-based suppression costs nothing and covers the worst of the
        # web: if the page's own domain is in a category this user blocks,
        # its imagery goes too — no model needed and no chance of a model
        # being wrong.
        host = flow.request.pretty_host or ""
        if self.categories.blocked_categories_of(
                host, self.policy.blocked_categories_for(uid)):
            self._blank_image(flow)
            return True

        # And the catalogue's OWN judgement of the source, independent of
        # what this account blocks for browsing: a site classified adult or
        # immodest (celebrity sites are) hosts imagery to match, so an
        # account that asked for pictures to be filtered gets that site's
        # pictures hidden outright — no model, no borderline misses. Both
        # the image's host and the page it sits on count, because big sites
        # serve their pictures from CDNs.
        sources = IMMODEST_SOURCES.get(level)
        if sources:
            referer = flow.request.headers.get("referer", "")
            referer_host = urlsplit(referer).hostname or ""
            for candidate in (host, referer_host):
                if candidate and (self.categories.categories_of(candidate)
                                  & sources):
                    log.info("hid a picture from %s (source is %s)",
                             candidate,
                             ",".join(sorted(self.categories.categories_of(candidate) & sources)))
                    self._blank_image(flow)
                    return True
        return False

    def _image_settle(self, flow: http.HTTPFlow, uid: int, level: str,
                      verdict) -> bool:
        """After the verdict: True when the response is settled, False when
        the picture is to be covered (verdict.regions say where)."""
        body = flow.response.content or b""
        # Cheap: it only writes when the answer has changed.
        self.vision.write_status()
        if verdict is None:
            # Could not look — no model installed, or it took too long.
            # Hiding is the safe direction, and it is the honest one: an
            # account that asked for pictures to be checked should not
            # quietly get unchecked pictures.
            self._blank_image(flow)
            return True
        # Image-search thumbnails are aggregated pictures of the whole web,
        # shrunk past what the detector can reliably judge — beach and
        # sheer-fabric shots sailed through at "immodest". At the modesty
        # levels, a PERSON in a search thumbnail is reason enough to hide
        # it; a landscape or product shot passes untouched.
        if level in ("immodest", "suggestive", "people") and verdict.has_person \
                and _is_search_thumb(flow.request.pretty_host or ""):
            log.info("hid a search thumbnail with a person for uid=%s", uid)
            self._blank_image(flow)
            return True
        if not vision_mod.hides(level, verdict):
            # The immodest level is the weak one: the detector has no
            # label for a bare arm, so a clothed model in a lingerie
            # catalogue comes back clean. The page's own words and the
            # presence of a person together catch most of that.
            if not vision_mod.in_context(verdict, self._referring_page_level(flow),
                                         CONTENT_TOLERANCE.get(level, "nsfw")):
                self._remember_clean(flow, level)
                return True
            log.info("hid a picture on a page that reads as %s",
                     self._referring_page_level(flow))
            self._blank_image(flow)
            return True
        # An animated picture (GIF, animated PNG or WebP) was judged on
        # frames spread through it; a cover on one frame means nothing on
        # the others, so it is hidden whole.
        if imageedit_mod.is_animated(body):
            log.info("hid an animated picture that reads as %s", verdict.level)
            self._blank_image(flow)
            return True
        # When the cover would take most of the picture, a frosted rectangle
        # in a frame of background helps nobody and costs a decode, a blur
        # and a re-encode. Hide it whole, instantly. (Not for the skin
        # style, whose whole point is to leave the rest of the figure.)
        if self._style_for(uid) == imageedit_mod.FROST and \
                imageedit_mod.dominant(body, verdict.regions):
            self._blank_image(flow)
            return True
        return False

    def _style_for(self, uid: int) -> str:
        """How this account wants a kept picture covered (see imageedit)."""
        getter = getattr(self.policy, "cover_style_for", None)
        return getter(uid) if getter else imageedit_mod.FROST

    def _cover(self, body: bytes, verdict, style: str = imageedit_mod.FROST) -> bytes | None:
        """The covered picture, from the cache of recent covers when the
        same picture came past a moment ago. CPU work: run on a worker."""
        cache_key = vision_mod.digest(body) + "|" + style + "|" + ",".join(
            "-".join(str(v) for v in box) for box in verdict.regions)
        covered = self.covers.get(cache_key)
        if covered is None:
            covered = imageedit_mod.cover(body, verdict.regions, style=style)
            if covered is not None:
                self.covers.put(cache_key, covered)
        return covered

    def _apply_cover(self, flow: http.HTTPFlow, verdict, covered: bytes | None) -> None:
        # Cover only what was found, so the rest of the picture — and the
        # page's layout — survives. If that cannot be done, hide it all.
        if covered is None:
            self._blank_image(flow)
            return
        flow.response.content = covered
        flow.response.headers["content-type"] = imageedit_mod.content_type(covered)
        flow.response.headers["x-kosheros"] = f"image-covered={verdict.level}"
        # The body changed; a length header on it would be a lie the
        # browser enforces.
        flow.response.headers.pop("content-length", None)

    def _filter_image(self, flow: http.HTTPFlow, uid: int) -> None:
        """The picture path, synchronously (tests and tools)."""
        level = self.policy.media_level_for(uid)
        if level == "none" or self._image_prejudge(flow, uid, level):
            return
        body = flow.response.content or b""
        verdict = self.vision.verdict(body)
        if self._image_settle(flow, uid, level, verdict):
            return
        self._apply_cover(flow, verdict, self._cover(body, verdict, self._style_for(uid)))

    async def _filter_image_async(self, flow: http.HTTPFlow, uid: int) -> None:
        """The picture path as the proxy runs it: the model and the cover
        both on a worker, the event loop free for everyone else meanwhile."""
        level = self.policy.media_level_for(uid)
        if level == "none" or self._image_prejudge(flow, uid, level):
            return
        body = flow.response.content or b""
        verdict = await self.vision.verdict_async(body)
        if self._image_settle(flow, uid, level, verdict):
            return
        covered = await self.vision.run_async(self._cover, body, verdict,
                                              self._style_for(uid))
        self._apply_cover(flow, verdict, covered)

    # ---- video -------------------------------------------------------------

    def _checker(self):
        return getattr(self, "videos", None)

    def _video_decision(self, flow: http.HTTPFlow, uid: int) -> str:
        """From the headers: "stream" (let through), "refuse", or "hold"
        (buffer the clip and sample it).

        YouTube is handled separately and precisely (categories and an
        approved-channel list). This is everything else.
        """
        level = self.policy.media_level_for(uid)
        if level == "none":
            return "stream"
        host = flow.request.pretty_host or ""
        if self.categories.blocked_categories_of(
                host, self.policy.blocked_categories_for(uid)):
            return "refuse"  # a clip from a site this account blocks
        if level not in VIDEO_CHECKED_LEVELS:
            return "refuse"  # "all": no video, as no pictures
        checker = self._checker()
        uncheckable = "refuse" if level in VIDEO_STRICT_LEVELS else "stream"
        if checker is None or not checker.available:
            return uncheckable
        _first, total = _range_info(flow)
        cached = checker.cached(videocheck_mod.key(flow.request.pretty_url, total))
        if cached is not None:
            return "refuse" if vision_mod.hides(level, cached) else "stream"
        length = _content_length(flow)
        if length is not None and length > videocheck_mod.VIDEO_MAX_BYTES:
            return uncheckable  # too big to hold for sampling
        # Held whether or not it starts at byte 0. A range from the middle
        # used to be refused outright, and a browser that asks for a clip
        # in pieces — Firefox does, and cancels its first request as soon
        # as it has the header — never got the clip judged and then got
        # every piece refused: "breaking and stuck". A fragment of a
        # fragmented file (DASH, a WebM cluster) decodes on its own and is
        # judged; a slice of a plain file cannot be read, and _video_settle
        # treats that as unchecked.
        return "hold"

    def _refuse_video(self, flow: http.HTTPFlow, before_body: bool = False) -> None:
        log.info("refused video %s", flow.request.pretty_url)
        try:
            page = self._page_of(flow)
        except Exception:  # noqa: BLE001 - the log is a convenience
            page = ""
        self._note(activity_mod.VIDEO, flow, url=page)
        if before_body:
            # Headers not yet sent: turn this response into the refusal and
            # drop the body as it arrives instead of downloading it.
            flow.response.status_code = 403
            flow.response.headers.clear()
            flow.response.headers["content-type"] = "text/plain"
            flow.response.headers["x-kosheros"] = "video-blocked"
            flow.response.stream = lambda chunk: b""
            return
        flow.response = http.Response.make(
            403, b"", {"Content-Type": "text/plain",
                       "x-kosheros": "video-blocked"})

    def _video_settle(self, flow: http.HTTPFlow, uid: int, level: str, verdict) -> None:
        if verdict is None:
            # Could not look: unreadable, or too slow. Refused at the
            # levels that refuse unchecked video, passed at the mildest —
            # the same answer the headers give a clip that cannot be
            # checked, so a clip is not passed from its headers and then
            # refused from its body.
            if level in VIDEO_STRICT_LEVELS:
                self._refuse_video(flow)
            else:
                flow.response.headers["x-kosheros"] = "video-unchecked"
            return
        if vision_mod.hides(level, verdict):
            self._refuse_video(flow)
            return
        flow.response.headers["x-kosheros"] = f"video-checked={verdict.level}"

    def _playlist_refused(self, flow: http.HTTPFlow, uid: int) -> bool:
        """A manifest is text pointing at segments, and the segments are
        what gets judged. Only a blocked source, and the levels with no
        video at all, refuse the manifest itself."""
        level = self.policy.media_level_for(uid)
        if level == "none":
            return False
        if self.categories.blocked_categories_of(
                flow.request.pretty_host or "",
                self.policy.blocked_categories_for(uid)):
            return True
        return level not in VIDEO_CHECKED_LEVELS

    def _filter_video(self, flow: http.HTTPFlow, uid: int) -> None:
        """The video path, synchronously (tests and tools)."""
        if _is_playlist(flow):
            if self._playlist_refused(flow, uid):
                self._refuse_video(flow)
            return
        decision = self._video_decision(flow, uid)
        if decision == "stream":
            return
        if decision == "refuse":
            self._refuse_video(flow)
            return
        level = self.policy.media_level_for(uid)
        _first, total = _range_info(flow)
        verdict = self._checker().verdict(
            flow.response.content or b"", videocheck_mod.key(flow.request.pretty_url, total))
        self._video_settle(flow, uid, level, verdict)

    async def _filter_video_async(self, flow: http.HTTPFlow, uid: int) -> None:
        if _is_playlist(flow):
            if self._playlist_refused(flow, uid):
                self._refuse_video(flow)
            return
        decision = self._video_decision(flow, uid)
        if decision == "stream":
            return
        if decision == "refuse":
            self._refuse_video(flow)
            return
        level = self.policy.media_level_for(uid)
        _first, total = _range_info(flow)
        verdict = await self._checker().verdict_async(
            flow.response.content or b"", videocheck_mod.key(flow.request.pretty_url, total))
        self._video_settle(flow, uid, level, verdict)

    # -- pictures judged clean a moment ago ----------------------------------------
    #
    # Every picture on a page is held until the model has looked at it, and
    # a shop's search page has sixty; on a machine that judges one in a
    # fifth of a second they arrive in a burst ten seconds long, each moving
    # the page as it lands. Most of them are pictures this machine judged
    # minutes ago — a shop reuses its images across every page and every
    # reload — but the verdict cache is keyed by content, and the content is
    # what the hold waits for. So a URL whose picture was judged clean is
    # remembered for a while, and the next request for it is streamed
    # straight through from the headers.
    #
    # The trust is narrow: the exact URL, the same media level, judged clean
    # by content within CLEAN_URL_SECONDS. A picture that was hidden or
    # covered is never remembered. An image CDN changing what a URL serves
    # inside that window is possible and rare; the window is short for it.
    CLEAN_URL_SECONDS = 15 * 60
    MAX_CLEAN_URLS = 4000

    def _remember_clean(self, flow: http.HTTPFlow, level: str) -> None:
        url = getattr(flow.request, "pretty_url", None)
        if not url:
            return
        memo = self.__dict__.setdefault("_clean_urls", {})
        memo[(url, level)] = time.monotonic()
        while len(memo) > self.MAX_CLEAN_URLS:
            memo.pop(next(iter(memo)))

    def _recently_clean(self, flow: http.HTTPFlow, level: str) -> bool:
        url = getattr(flow.request, "pretty_url", None)
        memo = self.__dict__.get("_clean_urls")
        if not url or not memo:
            return False
        when = memo.get((url, level))
        if when is None:
            return False
        if time.monotonic() - when > self.CLEAN_URL_SECONDS:
            memo.pop((url, level), None)
            return False
        return True

    def _blank_image(self, flow: http.HTTPFlow) -> None:
        # The same size as the picture it replaces, when that can be read:
        # a 1x1 collapsed the picture's box, and on a shop's grid every
        # picture that arrived after it moved the page — the scroll felt
        # jumpy. The tile is neutral grey; the 1x1 stays for a picture whose
        # size cannot be read.
        tile = imageedit_mod.placeholder_for(flow.response.content or b"")
        flow.response.content = tile if tile is not None else BLANK_PNG
        flow.response.headers["content-type"] = "image/png"
        flow.response.headers["x-kosheros"] = "image-hidden"
        flow.response.headers.pop("content-length", None)
        self._note_picture(flow)

    def _note_picture(self, flow: http.HTTPFlow) -> None:
        """Write down that pictures were hidden on the page this one is on."""
        try:
            page = self._page_of(flow)
            uid = flow.metadata.get("kosher_uid")
            now = time.monotonic()
            notes = self.__dict__.setdefault("_picture_notes", {})
            last = notes.get((uid, page))
            if last is not None and now - last < self.PICTURE_NOTE_SECONDS:
                return
            notes[(uid, page)] = now
            while len(notes) > self.MAX_PICTURE_NOTES:
                notes.pop(next(iter(notes)))
        except Exception:  # noqa: BLE001 - the log is a convenience
            return
        self._note(activity_mod.PICTURES, flow, url=page)

    @staticmethod
    def _page_of(flow: http.HTTPFlow) -> str:
        """The page a picture or video was on, as far as the browser says."""
        referer = flow.request.headers.get("referer") or ""
        return (referer or flow.request.pretty_url).split("#", 1)[0]

    def _filter_youtube(self, flow: http.HTTPFlow, uid: int) -> None:
        settings = self.policy.youtube_for(uid)
        allowed = settings.get("allowed_channels") or []
        blocked = settings.get("blocked_categories") or []
        path = flow.request.path or ""
        if not allowed and not blocked:
            # Said once per video rather than once per request: when a
            # person reports "YouTube still is not blocking", the first
            # thing to learn is whether the account's limits reached the
            # proxy at all, and this is the line that answers it.
            if YouTube.is_player_api(path):
                log.info("a YouTube video played: no YouTube limits are set for uid=%s "
                         "(the rules file has %s)", uid, settings or "nothing for this account")
            return

        no_shorts = YouTube.SHORTS_KIND in blocked
        if YouTube.is_player_api(path) or (no_shorts and YouTube.is_reel_api(path)):
            if no_shorts and YouTube.playing_a_short(flow):
                self._refuse_youtube_player(flow, "Shorts are turned off on this computer",
                                            "shorts")
                return
            self._filter_youtube_player(flow, allowed, blocked)
            return
        if YouTube.is_feed_api(path):
            # Every feed, not only the ones whose entries need dropping:
            # the hover players come out for any account with YouTube
            # limits, since a preview answers to none of them.
            self._filter_youtube_feed(flow, set(allowed), no_shorts)
            return
        if not any(path.startswith(p) for p in YouTube.WATCH_PATHS):
            return

        content_type = (flow.response.headers.get("content-type") or "").lower()
        if "text/html" not in content_type:
            return
        if no_shorts and YouTube.is_shorts_page(path):
            self._block(flow, flow.request.pretty_url,
                        " because Shorts are turned off", why="youtube:shorts")
            return
        body = flow.response.get_text(strict=False) or ""
        why = self._youtube_verdict(body, allowed, blocked)
        if why:
            self._block(flow, flow.request.pretty_url, why,
                        why="youtube:" + ("channel" if "channels" in why
                                          else "category"))

    @staticmethod
    def _youtube_verdict(body: str, allowed: list, blocked: list,
                         strict_unknown: bool = False) -> str | None:
        """Why this video may not be watched, or None.

        `strict_unknown`: a video whose category cannot be read counts as
        blocked. Right for the player JSON, where the microformat always
        names one and its absence means something is being hidden; wrong
        for a page's HTML, which may simply not carry it.
        """
        if allowed:
            channel_id, handle = YouTube.channel_of(body)
            if not ({channel_id, handle} & set(allowed)):
                return " because only approved channels are allowed"
        kinds = set(blocked) - {YouTube.SHORTS_KIND}
        if kinds:
            category = YouTube.category_of(body)
            if category and category in kinds:
                return " because that kind of video is turned off"
            if category is None and strict_unknown:
                return " because the video's kind could not be checked"
        return None

    def _filter_youtube_feed(self, flow: http.HTTPFlow, allowed: set,
                             no_shorts: bool = False) -> None:
        """Take videos out of the feeds that this account cannot play.

        Playback is already blocked; this is about what a child SEES. A
        home page full of videos that all fail teaches them the computer
        is broken rather than that somebody chose this.
        """
        body = flow.response.get_text(strict=False) or ""
        if not body or len(body) > suggest_mod.MAX_BYTES * 8:
            return
        try:
            document = json.loads(body)
        except ValueError:
            return
        pruned = YouTube.prune_feed(document, allowed) if allowed else document
        if no_shorts:
            pruned = YouTube.prune_shorts(pruned)
        pruned = YouTube.prune_previews(pruned)
        if pruned == document:
            return
        flow.response.text = json.dumps(pruned, separators=(",", ":"))
        flow.response.headers["x-kosheros"] = "youtube-feed-filtered"
        flow.response.headers.pop("content-length", None)

    def _filter_youtube_player(self, flow: http.HTTPFlow, allowed: list,
                               blocked: list) -> None:
        """The JSON the app fetches for every video after the first.

        Answered with YouTube's own "cannot be played" shape rather than a
        block page, because this response is consumed by the player, not
        shown to a person: a 403 here spins forever, and an HTML page in
        place of JSON is a broken app.
        """
        body = flow.response.get_text(strict=False) or ""
        if not body:
            log.info("a YouTube player answer had no body to read (%s); the video played",
                     (flow.request.path or "").split("?", 1)[0])
            return
        why = self._youtube_verdict(body, allowed, blocked, strict_unknown=True)
        if not why:
            log.info("a YouTube video played: its kind is %s; kinds turned off: %s",
                     YouTube.category_of(body) or "unreadable", sorted(blocked) or "none")
            return
        reason = ("Only approved channels can be watched on this computer"
                  if "approved channels" in why
                  else "That kind of video is turned off on this computer")
        self._refuse_youtube_player(flow, reason, "channel" if "channels" in why
                                    else "category")

    def _refuse_youtube_player(self, flow: http.HTTPFlow, reason: str, kind: str) -> None:
        log.info("blocked a YouTube video (%s)", kind)
        flow.response.text = json.dumps(YouTube.unplayable(reason))
        flow.response.headers["content-type"] = "application/json"
        flow.response.headers["x-kosheros"] = "youtube-blocked"
        flow.response.headers.pop("content-length", None)
        self._note(activity_mod.BLOCK, flow, url=self._page_of(flow),
                   why=f"youtube:{kind}")

    def _block(self, flow: http.HTTPFlow, url: str, because: str,
               why: str = "") -> None:
        """Refuse a page, tell the person, and write it down.

        `because` is the sentence on the block page; `why` is the same
        fact for machines ("category:video"), which the activity log keeps
        so the admin app can show the parent what was blocked and offer to
        allow it, and which rides along with a request so the parent sees
        why the page was refused when deciding.
        """
        flow.response = http.Response.make(
            403,
            BLOCK_PAGE.format(style=BLOCK_STYLE, url=_escape(url),
                              because=_escape(because), why=_escape(why),
                              request_path=REQUEST_PATH).encode(),
            {"Content-Type": "text/html; charset=utf-8"},
        )
        self._note(activity_mod.BLOCK, flow, url=url, why=why)

    def _note(self, kind: str, flow: http.HTTPFlow, **fields) -> None:
        """One line in the activity log. Never lets a logging problem
        become a filtering problem."""
        try:
            metadata = getattr(flow, "metadata", None) or {}
            activity_mod.record("proxy", kind, metadata.get("kosher_uid"),
                                **fields)
        except Exception:  # noqa: BLE001 - the log is a convenience
            log.debug("could not record activity", exc_info=True)

    # A page's hidden pictures are one line, not one per picture: a shop
    # page with forty thumbnails would otherwise write forty lines that all
    # say the same thing. Re-noted after ten minutes so a page someone keeps
    # coming back to still shows up.
    PICTURE_NOTE_SECONDS = 600
    MAX_PICTURE_NOTES = 256

    def _handle_request_form(self, flow: http.HTTPFlow, uid) -> None:
        """Take a request for access off the block page.

        Answered here and never forwarded: the address is reserved and
        this machine is the only thing that will ever see it.
        """
        from urllib.parse import parse_qs

        heading, message = "Could not ask", "Please try again."
        try:
            form = parse_qs(flow.request.get_text(strict=False) or "")
            url = (form.get("url") or [""])[0]
            note = (form.get("note") or [""])[0]
            why = (form.get("why") or [""])[0]
            if uid is None:
                raise accessreq_mod.RequestError("could not tell who is asking")
            accessreq_mod.submit(uid, url, note, why=why)
        except accessreq_mod.RequestError as e:
            message = _escape(str(e))
        except Exception:  # noqa: BLE001 - a failed ask is not a crash
            log.exception("could not record an access request")
        else:
            heading = "Your request was sent"
            message = ("The administrator of this computer will see it. "
                       "Nothing has changed yet.")
        flow.response = http.Response.make(
            200,
            ASKED_PAGE.format(style=BLOCK_STYLE, heading=heading,
                              message=message).encode(),
            {"Content-Type": "text/html; charset=utf-8",
             "Cache-Control": "no-store"})


def _escape(text: str) -> str:
    return (text.replace("&", "&amp;").replace("<", "&lt;")
                .replace(">", "&gt;").replace('"', "&quot;"))[:300]


addons = [KosherFilter()]
