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
from urllib.parse import urlencode

from mitmproxy import http

import sys

try:
    from kosherd import categories as categories_mod
    from kosherd import content as content_mod
    from kosherd import elementfilter as elementfilter_mod
    from kosherd import imageedit as imageedit_mod
    from kosherd import language as language_mod
    from kosherd import accessreq as accessreq_mod
    from kosherd import search as search_mod
    from kosherd import siterules as siterules_mod
    from kosherd import suggest as suggest_mod
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
    from kosherd import search as search_mod
    from kosherd import siterules as siterules_mod
    from kosherd import suggest as suggest_mod
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


class PolicyCache:
    """Per-uid rules, reloaded when kosherd rewrites the rules file."""

    def __init__(self, path: Path = RULES_PATH):
        self.path = path
        self._mtime = 0.0
        self._rules: dict[int, list] = {}
        self._blocked: dict[int, list] = {}
        self._media: dict[int, str] = {}
        self._language: dict[int, str] = {}
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

    def language_filter_for(self, uid: int | None) -> str:
        self._refresh()
        return self._language.get(uid, "off") if uid is not None else "off"

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
        language: dict[int, str] = {}
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
                language[uid] = entry.get("language_filter", "off")
                youtube[uid] = dict(entry.get("youtube", {}))
            except Exception as e:  # noqa: BLE001 - one bad entry must not break all
                log.error("bad entry for uid %s: %s", uid_text, e)
        self._rules = rules
        self._blocked = blocked
        self._media = media
        self._language = language
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
BLANK_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)

IMAGE_TYPES = ("image/",)
# Video is judged by where it comes from, not by what is in it. Decoding
# frames would mean ffmpeg and a second or more per clip on a machine that
# may have two cores, for a stream the person is already watching — so the
# levers that work are the source (a blocked category), the page (scored by
# its words), the thumbnail (an image, filtered like any other) and, for
# YouTube, the category and channel limits above.
VIDEO_TYPES = ("video/", "application/vnd.apple.mpegurl",
               "application/x-mpegurl", "application/dash+xml")
# Levels that mean "no video from the open web".
VIDEO_BLOCKED_LEVELS = ("all", "immodest", "suggestive")
# Below this, an image is an icon, a spacer or a tracking pixel: not worth
# hiding and not worth a classifier's time.
MIN_IMAGE_BYTES = 6000


def _is_image(flow) -> bool:
    content_type = (flow.response.headers.get("content-type") or "").lower()
    return content_type.startswith(IMAGE_TYPES)


def _is_video(flow) -> bool:
    content_type = (flow.response.headers.get("content-type") or "").lower()
    return content_type.startswith(VIDEO_TYPES)


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

    HOSTS = ("youtube.com", "youtube-nocookie.com", "youtubekids.com")

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
    def restrict_header(settings: dict) -> str | None:
        level = (settings or {}).get("restrict", "moderate")
        return {"moderate": "Moderate", "strict": "Strict"}.get(level)

    # What the app shows when a video genuinely cannot be played. Reusing
    # YouTube's own shape means the page renders a normal message instead
    # of spinning forever or showing a broken player.
    @staticmethod
    def unplayable(reason: str) -> dict:
        return {
            "playabilityStatus": {
                "status": "ERROR",
                "reason": reason,
                "errorScreen": {"playerErrorMessageRenderer": {
                    "reason": {"simpleText": reason},
                    "subreason": {"simpleText":
                                  "Ask the administrator of this computer."},
                }},
            },
            "videoDetails": {},
        }

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


# How much a page may say before it is blocked, per media level — the same
# ladder the search results use, so a page reached by clicking a link and a
# page reached from search are judged alike.
CONTENT_TOLERANCE = {
    "none": content_mod.NSFW,
    "nsfw": content_mod.NSFW,
    "suggestive": content_mod.SUGGESTIVE,
    "immodest": content_mod.IMMODEST,
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
        self.siterules = siterules_mod.load()
        self.blocklist = search_mod.load_blocklist()

    def request(self, flow: http.HTTPFlow) -> None:
        # Everything reaching this proxy belongs to a filtered user: only
        # their traffic is redirected here.
        _force_safesearch(flow)

        client_port = flow.client_conn.peername[1] if flow.client_conn.peername else None
        uid = self.uids.uid_for_port(client_port) if client_port else None
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
            self._block(flow, url, "")
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
                self._block(flow, url, f" because it is {names}")
                return

            # A department on a shop the family uses. Read from the
            # address, so it costs microseconds and happens before the
            # page is fetched.
            if siterules_mod.GATING_CATEGORY in blocked:
                why = self.siterules.reason(url) or self._blocked_shop_search(url)
                if why:
                    log.info("blocked uid=%s %s (%s)", uid, url, why)
                    self._block(flow, url, f" because it is {why}")



    def response(self, flow: http.HTTPFlow) -> None:
        uid = flow.metadata.get("kosher_uid")
        if uid is None:
            return

        if _is_image(flow):
            self._filter_image(flow, uid)
            return

        if _is_video(flow):
            self._filter_video(flow, uid)
            return

        if YouTube.applies(flow.request.pretty_host):
            self._filter_youtube(flow, uid)
            return

        if self.siterules.is_suggestions(flow.request.pretty_url):
            self._filter_suggestions(flow, uid)
            return

        self._filter_page(flow, uid)

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
        if immodest_blocked:
            body, stripped = self._strip_shop_navigation(
                flow, flow.request.pretty_host or "", body)
        text = content_mod.visible_text(body[:MAX_SCORED_BYTES])

        if language_filter != "off" and self.wordlist.contains_any(text):
            if language_filter == "block":
                self._block(flow, flow.request.pretty_url,
                            " because of the language on it")
                return
            cleaned, count = language_mod.clean_html(body, self.wordlist)
            if count:
                flow.response.text = cleaned
                flow.response.headers["x-kosheros"] = f"language-cleaned={count}"
            # The page has been rewritten; score the cleaned copy below.
            text = content_mod.visible_text(cleaned[:MAX_SCORED_BYTES])

        if level == "none" and not immodest_blocked:
            return
        tolerance = self._content_tolerance(
            uid, flow.request.pretty_host or "", stripped)
        verdict = self.scorer.score(text)
        if verdict.at_least(tolerance):
            log.info("blocked uid=%s %s (content: %s, %d points, %s)",
                     uid, flow.request.pretty_url, verdict.level,
                     verdict.points, ", ".join(verdict.hits))
            self._block(flow, flow.request.pretty_url,
                        f" because the page reads as {verdict.level}")

    def _filter_image(self, flow: http.HTTPFlow, uid: int) -> None:
        level = self.policy.media_level_for(uid)
        if level == "none":
            return
        body = flow.response.content or b""
        if len(body) < MIN_IMAGE_BYTES:
            return  # icons, spacers, tracking pixels

        # "all" needs no judgement, which is why it is the only level that
        # is right every time.
        if level == "all":
            self._blank_image(flow)
            return

        # Source-based suppression costs nothing and covers the worst of the
        # web: if the page's own domain is in a category this user blocks,
        # its imagery goes too — no model needed and no chance of a model
        # being wrong.
        host = flow.request.pretty_host or ""
        if self.categories.blocked_categories_of(
                host, self.policy.blocked_categories_for(uid)):
            self._blank_image(flow)
            return

        verdict = self.vision.verdict(body)
        # Cheap: it only writes when the answer has changed.
        self.vision.write_status()
        if verdict is None:
            # Could not look — no model installed, or it took too long.
            # Hiding is the safe direction, and it is the honest one: an
            # account that asked for pictures to be checked should not
            # quietly get unchecked pictures.
            self._blank_image(flow)
            return
        if not vision_mod.hides(level, verdict):
            return

        # Cover only what was found, so the rest of the picture — and the
        # page's layout — survives. If that cannot be done, hide it all.
        covered = imageedit_mod.cover(body, verdict.regions)
        if covered is None:
            self._blank_image(flow)
            return
        flow.response.content = covered
        flow.response.headers["content-type"] = (
            "image/jpeg" if covered[:2] == b"\xff\xd8" else "image/png")
        flow.response.headers["x-kosheros"] = f"image-covered={verdict.level}"

    def _filter_video(self, flow: http.HTTPFlow, uid: int) -> None:
        """Video, judged by its source rather than its frames.

        YouTube is handled separately and precisely (categories and an
        approved-channel list). This is everything else: a clip from a
        site in a category this account blocks, or any clip at all for an
        account whose pictures are filtered, since a video is pictures at
        thirty a second and nothing here can look inside one in time.
        """
        level = self.policy.media_level_for(uid)
        if level == "none":
            return
        host = flow.request.pretty_host or ""
        from_blocked_source = bool(self.categories.blocked_categories_of(
            host, self.policy.blocked_categories_for(uid)))
        if level not in VIDEO_BLOCKED_LEVELS and not from_blocked_source:
            return
        log.info("blocked video uid=%s %s", uid, flow.request.pretty_url)
        flow.response = http.Response.make(
            403, b"", {"Content-Type": "text/plain",
                       "x-kosheros": "video-blocked"})

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
        if YouTube.is_player_api(path):
            self._filter_youtube_player(flow, allowed, blocked)
            return
        if not any(path.startswith(p) for p in YouTube.WATCH_PATHS):
            return

        content_type = (flow.response.headers.get("content-type") or "").lower()
        if "text/html" not in content_type:
            return
        body = flow.response.get_text(strict=False) or ""
        why = self._youtube_verdict(body, allowed, blocked)
        if why:
            self._block(flow, flow.request.pretty_url, why)

    @staticmethod
    def _youtube_verdict(body: str, allowed: list, blocked: list) -> str | None:
        """Why this video may not be watched, or None."""
        if allowed:
            channel_id, handle = YouTube.channel_of(body)
            if not ({channel_id, handle} & set(allowed)):
                return " because only approved channels are allowed"
        if blocked:
            category = YouTube.category_of(body)
            if category and category in blocked:
                return " because that kind of video is turned off"
        return None

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
            return
        why = self._youtube_verdict(body, allowed, blocked)
        if not why:
            return
        reason = ("Only approved channels can be watched on this computer"
                  if "approved channels" in why
                  else "That kind of video is turned off on this computer")
        log.info("blocked a YouTube video (%s)", why.strip())
        flow.response.text = json.dumps(YouTube.unplayable(reason))
        flow.response.headers["content-type"] = "application/json"
        flow.response.headers["x-kosheros"] = "youtube-blocked"
        flow.response.headers.pop("content-length", None)

    def _block(self, flow: http.HTTPFlow, url: str, because: str) -> None:
        flow.response = http.Response.make(
            403,
            BLOCK_PAGE.format(style=BLOCK_STYLE, url=_escape(url),
                              because=_escape(because),
                              request_path=REQUEST_PATH).encode(),
            {"Content-Type": "text/html; charset=utf-8"},
        )

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
            if uid is None:
                raise accessreq_mod.RequestError("could not tell who is asking")
            accessreq_mod.submit(uid, url, note)
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
