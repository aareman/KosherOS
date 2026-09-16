"""Tests for the mitmproxy addon's uid lookup and policy cache.

The addon itself imports mitmproxy, which the dev shell doesn't have, so we
test the two pieces that carry the real risk — parsing /proc/net/tcp and
reloading the policy — by loading the module with a stub in place.
"""

import asyncio
import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest

ADDON = Path(__file__).parents[2] / "mitm" / "kosher_filter.py"


@pytest.fixture
def addon(monkeypatch):
    """Import kosher_filter with mitmproxy stubbed out."""
    mitmproxy = types.ModuleType("mitmproxy")
    http_mod = types.ModuleType("mitmproxy.http")
    http_mod.HTTPFlow = object
    # A response object real enough to assert on: the addon replaces a
    # flow's response with one of these and then nothing else touches it.
    http_mod.Response = types.SimpleNamespace(
        make=lambda status=200, content=b"", headers=None: types.SimpleNamespace(
            status_code=status, content=content, headers=dict(headers or {})))
    mitmproxy.http = http_mod
    monkeypatch.setitem(sys.modules, "mitmproxy", mitmproxy)
    monkeypatch.setitem(sys.modules, "mitmproxy.http", http_mod)

    spec = importlib.util.spec_from_file_location("kosher_filter_under_test", ADDON)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_uid_lookup_reads_proc_net_tcp(addon, tmp_path):
    # Two sockets; the one on port 0xC000 (49152) belongs to uid 1001.
    proc = tmp_path / "tcp"
    proc.write_text(
        "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n"
        "   0: 0100007F:1F90 00000000:0000 0A 00000000:00000000 00:00000000 00000000  989        0 1\n"
        "   1: 0100007F:C000 5DB8D822:01BB 01 00000000:00000000 00:00000000 00000000 1001        0 2\n"
    )
    lookup = addon.UidLookup()
    lookup.PATHS = (str(proc),)
    assert lookup.uid_for_port(49152) == 1001
    # A listening socket is nobody's connection.
    assert lookup.uid_for_port(8080) is None
    assert lookup.uid_for_port(12345) is None


def test_uid_lookup_survives_missing_files(addon):
    lookup = addon.UidLookup()
    lookup.PATHS = ("/nonexistent/tcp",)
    assert lookup.uid_for_port(1234) is None


def _rules_file(tmp_path, mapping):
    path = tmp_path / "rules.json"
    path.write_text(json.dumps(mapping))
    return path


def test_policy_cache_loads_rules_per_uid(addon, tmp_path):
    path = _rules_file(tmp_path, {
        "1001": [{"action": "block", "pattern": "youtube.com/watch*"}],
        "1002": [],
    })
    cache = addon.PolicyCache(path)
    assert len(cache.rules_for(1001)) == 1
    assert cache.rules_for(1002) == []
    assert cache.rules_for(None) == []
    assert cache.rules_for(4242) == []


def test_policy_cache_includes_guest_uid(addon, tmp_path):
    path = _rules_file(tmp_path, {"1010": [{"action": "block", "pattern": "*"}]})
    assert len(addon.PolicyCache(path).rules_for(1010)) == 1


def test_policy_cache_reloads_on_change(addon, tmp_path):
    path = _rules_file(tmp_path, {"1001": []})
    cache = addon.PolicyCache(path)
    assert cache.rules_for(1001) == []
    import os
    import time
    path.write_text(json.dumps({"1001": [{"action": "block", "pattern": "bad.com"}]}))
    os.utime(path, (time.time() + 10, time.time() + 10))  # ensure mtime differs
    assert len(cache.rules_for(1001)) == 1


def test_policy_cache_tolerates_bad_rules(addon, tmp_path):
    path = _rules_file(tmp_path, {"1001": [{"action": "explode", "pattern": "x"}]})
    assert addon.PolicyCache(path).rules_for(1001) == []


def test_policy_cache_tolerates_missing_file(addon, tmp_path):
    assert addon.PolicyCache(tmp_path / "nope.json").rules_for(1001) == []


# -- categories ---------------------------------------------------------------

def test_cache_reads_rules_and_categories(addon, tmp_path):
    path = _rules_file(tmp_path, {
        "1001": {"rules": [{"action": "block", "pattern": "x.com"}],
                 "blocked_categories": ["adult", "gambling"]},
    })
    cache = addon.PolicyCache(path)
    assert len(cache.rules_for(1001)) == 1
    assert cache.blocked_categories_for(1001) == ["adult", "gambling"]


def test_cache_still_reads_the_older_rules_only_shape(addon, tmp_path):
    # A device that syncs a policy written by an older kosherd must not be
    # left with no rules at all.
    path = _rules_file(tmp_path, {"1001": [{"action": "block", "pattern": "x.com"}]})
    cache = addon.PolicyCache(path)
    assert len(cache.rules_for(1001)) == 1
    assert cache.blocked_categories_for(1001) == []


def test_unknown_users_fail_closed_not_open(addon, tmp_path):
    # Everything reaching the proxy is a filtered user; a uid we have no
    # entry for is one whose policy has not arrived, not a stranger. It
    # must get the safe floor, never the open web. This is the bug the
    # first family test hit — an account that showed as filtered and
    # filtered nothing.
    from kosherd.categories import DEFAULT_BLOCKED

    cache = addon.PolicyCache(_rules_file(tmp_path, {}))
    assert set(cache.blocked_categories_for(4242)) == set(DEFAULT_BLOCKED)
    assert set(cache.blocked_categories_for(None)) == set(DEFAULT_BLOCKED)
    assert cache.media_level_for(4242) == "immodest"
    assert cache.language_filter_for(None) == "substitute"
    assert cache.youtube_for(4242).get("restrict") == "strict"


def test_a_known_user_keeps_their_own_empty_choice(addon, tmp_path):
    # A uid we DO have an entry for is respected as written, even if empty:
    # the floor is a default for the absent, not an override of the known.
    path = _rules_file(tmp_path, {
        "1001": {"rules": [], "blocked_categories": [], "media_level": "none"}})
    cache = addon.PolicyCache(path)
    assert cache.blocked_categories_for(1001) == []
    assert cache.media_level_for(1001) == "none"


def test_a_missing_rules_file_fails_closed(addon, tmp_path):
    # No file at all (proxy started before kosherd wrote one) is the most
    # dangerous moment; it must not be an open window.
    from kosherd.categories import DEFAULT_BLOCKED

    cache = addon.PolicyCache(tmp_path / "nope.json")
    assert set(cache.blocked_categories_for(1001)) == set(DEFAULT_BLOCKED)


# -- media level and YouTube --------------------------------------------------

def test_cache_reads_media_level_and_youtube(addon, tmp_path):
    path = _rules_file(tmp_path, {"1001": {
        "rules": [], "blocked_categories": [], "media_level": "immodest",
        "youtube": {"allowed_channels": ["@torah"], "restrict": "strict"}}})
    cache = addon.PolicyCache(path)
    assert cache.media_level_for(1001) == "immodest"
    assert cache.youtube_for(1001)["restrict"] == "strict"
    # An unknown user is not "unaffected" — it fails closed to the floor.
    assert cache.media_level_for(4242) == "immodest"


def test_youtube_restrict_header_per_level(addon):
    assert addon.YouTube.restrict_header({"restrict": "strict"}) == "Strict"
    assert addon.YouTube.restrict_header({"restrict": "moderate"}) == "Moderate"
    # "none" means do not send the header at all, not send an empty one.
    assert addon.YouTube.restrict_header({"restrict": "none"}) is None
    assert addon.YouTube.restrict_header({}) == "Moderate"  # default


def test_youtube_recognises_its_own_hosts(addon):
    assert addon.YouTube.applies("www.youtube.com")
    assert addon.YouTube.applies("www.youtube-nocookie.com")
    assert not addon.YouTube.applies("youtube.evil.com.attacker.net")
    assert not addon.YouTube.applies("chinuch.org")


def test_youtube_category_and_channel_are_read_from_the_page(addon):
    body = ('{"videoDetails":{"channelId":"UC123","title":"x"},'
            '"category":"27","canonicalBaseUrl":"/@torahchannel"}')
    assert addon.YouTube.category_of(body) == "27"
    channel_id, handle = addon.YouTube.channel_of(body)
    assert channel_id == "UC123"
    assert handle == "@torahchannel"


def test_youtube_metadata_missing_is_not_a_crash(addon):
    assert addon.YouTube.category_of("<html>nothing here</html>") is None
    assert addon.YouTube.channel_of("") == (None, None)


def test_the_placeholder_is_a_real_png(addon):
    # A broken image icon makes a page look broken; a valid 1x1 keeps layout.
    assert addon.BLANK_PNG[:8] == b"\x89PNG\r\n\x1a\n"


# -- sending searches to the local, filtered search page ----------------------

class _Req:
    def __init__(self, host, path, query=None):
        self.pretty_host = host
        self.path = path
        self.query = query or {}


class _Flow:
    def __init__(self, host, path, query=None):
        self.request = _Req(host, path, query)


def test_a_search_result_page_is_recognised(addon):
    assert addon._search_query(_Flow("www.google.com", "/search",
                                     {"q": "kosher recipes"})) == "kosher recipes"
    assert addon._search_query(_Flow("duckduckgo.com", "/",
                                     {"q": "chinuch"})) == "chinuch"
    assert addon._search_query(_Flow("yandex.com", "/search",
                                     {"text": "shiur"})) == "shiur"


def test_the_rest_of_a_search_engine_is_left_alone(addon):
    # Redirecting these breaks the site rather than filtering it.
    assert addon._search_query(_Flow("www.google.com", "/maps",
                                     {"q": "pizza"})) is None
    assert addon._search_query(_Flow("www.google.com", "/complete/search",
                                     {"q": "k"})) is None
    assert addon._search_query(_Flow("www.google.com", "/", {})) is None


def test_other_sites_are_not_treated_as_searches(addon):
    assert addon._search_query(_Flow("chinuch.org", "/search",
                                     {"q": "parsha"})) is None
    assert addon._search_query(_Flow("googleusercontent.evil.example",
                                     "/search", {"q": "x"})) is None


# -- judging a page by its words ----------------------------------------------

def test_cache_reads_the_language_filter(addon, tmp_path):
    path = _rules_file(tmp_path, {"1001": {"rules": [], "language_filter": "block"}})
    cache = addon.PolicyCache(path)
    assert cache.language_filter_for(1001) == "block"
    # Unknown user fails closed to substitution, not "off".
    assert cache.language_filter_for(4242) == "substitute"


def test_page_scoring_uses_the_same_ladder_as_search(addon):
    # A page reached by clicking a link and a page reached from search
    # must be judged alike, or the filter is arbitrary.
    from kosherd import search as search_mod
    assert addon.CONTENT_TOLERANCE == search_mod.CONTENT_TOLERANCE


# -- pictures -----------------------------------------------------------------

def test_the_proxy_hides_a_picture_it_could_not_judge(addon):
    # No model installed, or inference timed out. An account that asked for
    # pictures to be checked must not quietly get unchecked pictures.
    from kosherd import vision

    filt = addon.KosherFilter.__new__(addon.KosherFilter)
    filt.policy = _stub_policy(media="immodest")
    filt.categories = _stub_categories()
    filt.page_levels = {}
    filt.vision = _stub_vision(None)
    flow = _image_flow()
    filt._filter_image(flow, 1001)
    assert flow.response.content == addon.BLANK_PNG


def test_a_clean_picture_is_left_alone(addon):
    from kosherd import vision

    filt = addon.KosherFilter.__new__(addon.KosherFilter)
    filt.policy = _stub_policy(media="immodest")
    filt.categories = _stub_categories()
    filt.page_levels = {}
    filt.vision = _stub_vision(vision.ImageVerdict(vision.CLEAN, ()))
    flow = _image_flow()
    original = flow.response.content
    filt._filter_image(flow, 1001)
    assert flow.response.content == original


def test_hiding_everything_never_consults_the_model(addon):
    class Never:
        def verdict(self, data):
            raise AssertionError("'all' needs no judgement")

    filt = addon.KosherFilter.__new__(addon.KosherFilter)
    filt.policy = _stub_policy(media="all")
    filt.categories = _stub_categories()
    filt.vision = Never()
    flow = _image_flow()
    filt._filter_image(flow, 1001)
    assert flow.response.content == addon.BLANK_PNG


class _Resp:
    """Enough of mitmproxy's Response for the addon to read and rewrite."""

    def __init__(self, content, headers):
        self.content = content
        self.headers = headers

    def get_text(self, strict=False):
        body = self.content
        return body.decode("utf-8", "replace") if isinstance(body, bytes) else body

    @property
    def text(self):
        return self.get_text()

    @text.setter
    def text(self, value):
        self.content = value.encode()


def _image_flow():
    return type("F", (), {
        "request": type("R", (), {"pretty_host": "example.com",
                                  "headers": {}})(),
        "response": _Resp(b"\x89PNG" + b"x" * 20_000,
                          {"content-type": "image/png"}),
    })()


def _stub_policy(media="none", blocked=()):
    return type("P", (), {
        "uid_for_listener_port": staticmethod(lambda port: None),
        "media_level_for": staticmethod(lambda uid: media),
        "blocked_categories_for": staticmethod(lambda uid: list(blocked)),
    })()


def _stub_blocklist():
    return type("B", (), {"contains_any": staticmethod(lambda text: False)})()


def _stub_categories(source_cats=()):
    return type("C", (), {
        "blocked_categories_of": staticmethod(lambda host, blocked: set()),
        "categories_of": staticmethod(lambda host: set(source_cats)),
    })()


def _stub_vision(verdict):
    return type("V", (), {
        "verdict": staticmethod(lambda data: verdict),
        "write_status": staticmethod(lambda *a, **k: None),
    })()


# -- video --------------------------------------------------------------------

def test_video_is_recognised_including_its_playlists(addon):
    for kind in ("video/mp4", "application/vnd.apple.mpegurl",
                 "application/dash+xml"):
        assert addon._is_video(_typed_flow(kind)), kind
    assert not addon._is_video(_typed_flow("text/html"))
    assert not addon._is_video(_typed_flow("image/png"))


def test_an_account_with_picture_filtering_gets_no_open_web_video(addon):
    # A video is pictures at thirty a second, and nothing here can look
    # inside one in time.
    filt = addon.KosherFilter.__new__(addon.KosherFilter)
    filt.policy = _stub_policy(media="immodest")
    filt.categories = _stub_categories()
    flow = _typed_flow("video/mp4")
    filt._filter_video(flow, 1001)
    assert flow.response.headers["x-kosheros"] == "video-blocked"


def test_video_is_left_alone_when_pictures_are(addon):
    filt = addon.KosherFilter.__new__(addon.KosherFilter)
    filt.policy = _stub_policy(media="none")
    filt.categories = _stub_categories()
    flow = _typed_flow("video/mp4")
    before = flow.response
    filt._filter_video(flow, 1001)
    assert flow.response is before


def test_video_from_a_blocked_category_goes_even_at_the_mildest_level(addon):
    filt = addon.KosherFilter.__new__(addon.KosherFilter)
    filt.policy = _stub_policy(media="nsfw", blocked=["adult"])
    filt.categories = type("C", (), {
        "blocked_categories_of": staticmethod(lambda host, blocked: {"adult"}),
    })()
    flow = _typed_flow("video/mp4")
    filt._filter_video(flow, 1001)
    assert flow.response.headers["x-kosheros"] == "video-blocked"


def _typed_flow(content_type):
    return type("F", (), {
        "request": type("R", (), {"pretty_host": "example.com",
                                  "pretty_url": "https://example.com/v.mp4"})(),
        "response": _Resp(b"\x00" * 100, {"content-type": content_type}),
    })()


# -- shop departments ---------------------------------------------------------

def test_the_proxy_blocks_a_department_before_fetching_the_page(addon):
    from kosherd import siterules
    from pathlib import Path

    rules = siterules.load(
        Path(__file__).parents[2] / "os-image/files/usr/share/kosher/site-rules.json")
    filt = addon.KosherFilter.__new__(addon.KosherFilter)
    filt.uids = type("U", (), {"uid_for_connection": staticmethod(lambda *a, **k: 1001)})()
    filt.policy = _request_policy(blocked=["immodest"])
    filt.categories = _stub_categories()
    filt.siterules = rules
    filt.blocklist = _stub_blocklist()

    blocked = _request_flow("https://www.amazon.com/s?k=x&i=fashion-womens")
    filt.request(blocked)
    assert blocked.response is not None
    assert blocked.response.status_code == 403

    allowed = _request_flow("https://www.amazon.com/s?k=laptop")
    filt.request(allowed)
    assert allowed.response is None


def test_a_department_is_only_blocked_for_accounts_that_asked(addon):
    from kosherd import siterules
    from pathlib import Path

    rules = siterules.load(
        Path(__file__).parents[2] / "os-image/files/usr/share/kosher/site-rules.json")
    filt = addon.KosherFilter.__new__(addon.KosherFilter)
    filt.uids = type("U", (), {"uid_for_connection": staticmethod(lambda *a, **k: 1001)})()
    filt.policy = _request_policy(blocked=["gambling"])
    filt.categories = _stub_categories()
    filt.siterules = rules
    filt.blocklist = _stub_blocklist()

    flow = _request_flow("https://www.amazon.com/s?k=x&i=fashion-womens")
    filt.request(flow)
    assert flow.response is None


def _request_policy(blocked=()):
    return type("P", (), {
        "uid_for_listener_port": staticmethod(lambda port: None),
        "rules_for": staticmethod(lambda uid: []),
        "blocked_categories_for": staticmethod(lambda uid: list(blocked)),
        "youtube_for": staticmethod(lambda uid: {}),
    })()


def _request_flow(url):
    from urllib.parse import urlsplit

    parts = urlsplit(url)
    return type("F", (), {
        "request": type("R", (), {
            "pretty_host": parts.hostname,
            "pretty_url": url,
            "path": parts.path + (f"?{parts.query}" if parts.query else ""),
            "method": "GET",
            "query": {},
            "headers": {},
        })(),
        "response": None,
        "client_conn": type("C", (), {"peername": ("127.0.0.1", 40000)})(),
        "metadata": {},
    })()


# -- asking for a page --------------------------------------------------------

def test_the_block_page_offers_a_way_to_ask(addon):
    filt = addon.KosherFilter.__new__(addon.KosherFilter)
    flow = _request_flow("https://example.com/x")
    filt._block(flow, "https://example.com/x", " because it is adult")
    body = flow.response.content.decode()
    assert addon.REQUEST_PATH in body
    assert "Ask for this page" in body
    assert "Nothing changes until they say yes" in body


def test_the_ask_form_posts_to_the_same_origin(addon):
    # A local http:// address would be mixed content from a page the
    # browser considers https, and browsers refuse to submit that.
    filt = addon.KosherFilter.__new__(addon.KosherFilter)
    flow = _request_flow("https://example.com/x")
    filt._block(flow, "https://example.com/x", "")
    assert f'action="{addon.REQUEST_PATH}"' in flow.response.content.decode()
    assert "http://" not in flow.response.content.decode()


def test_the_reserved_path_is_answered_here_and_never_forwarded(addon, tmp_path):
    from kosherd import accessreq

    filt = addon.KosherFilter.__new__(addon.KosherFilter)
    filt.uids = type("U", (), {"uid_for_connection": staticmethod(lambda *a, **k: 1001)})()
    filt.policy = _request_policy()
    filt.categories = _stub_categories()
    filt.siterules = type("S", (), {
        "reason": staticmethod(lambda u: None),
        "blocked_search": staticmethod(lambda u: None),
        "search_text": staticmethod(lambda u: None),
    })()
    filt.blocklist = _stub_blocklist()

    flow = _request_flow("https://example.com" + addon.REQUEST_PATH)
    flow.request.method = "POST"
    flow.request.get_text = lambda strict=False: (
        "url=https%3A%2F%2Fexample.com%2Fneeded&note=for+school")
    addon.accessreq_mod.SPOOL_DIR = tmp_path
    import functools
    addon.accessreq_mod.submit = functools.partial(accessreq.submit, spool=tmp_path)

    filt.request(flow)
    assert flow.response is not None
    assert "Your request was sent" in flow.response.content.decode()
    waiting = accessreq.pending(spool=tmp_path)
    assert len(waiting) == 1
    assert waiting[0]["url"] == "https://example.com/needed"
    assert waiting[0]["note"] == "for school"
    assert waiting[0]["uid"] == 1001


def test_a_get_to_the_reserved_path_is_an_ordinary_request(addon):
    # Only a POST is ours; a site with a page at that path still works.
    filt = addon.KosherFilter.__new__(addon.KosherFilter)
    filt.uids = type("U", (), {"uid_for_connection": staticmethod(lambda *a, **k: 1001)})()
    filt.policy = _request_policy()
    filt.categories = _stub_categories()
    filt.siterules = type("S", (), {
        "reason": staticmethod(lambda u: None),
        "blocked_search": staticmethod(lambda u: None),
        "search_text": staticmethod(lambda u: None),
    })()
    filt.blocklist = _stub_blocklist()
    flow = _request_flow("https://example.com" + addon.REQUEST_PATH)
    flow.request.method = "GET"
    filt.request(flow)
    assert flow.response is None


# -- a shop's own search box and autocomplete ---------------------------------

def _shop_filter(addon, blocked=("immodest",)):
    from pathlib import Path

    from kosherd import content, search as search_mod, siterules

    root = Path(__file__).parents[2]
    filt = addon.KosherFilter.__new__(addon.KosherFilter)
    filt.uids = type("U", (), {"uid_for_connection": staticmethod(lambda *a, **k: 1001)})()
    filt.policy = type("P", (), {
        "uid_for_listener_port": staticmethod(lambda port: None),
        "rules_for": staticmethod(lambda uid: []),
        "blocked_categories_for": staticmethod(lambda uid: list(blocked)),
        "media_level_for": staticmethod(lambda uid: "none"),
        "language_filter_for": staticmethod(lambda uid: "off"),
        "youtube_for": staticmethod(lambda uid: {}),
    })()
    filt.categories = _stub_categories()
    filt.siterules = siterules.load(
        root / "os-image/files/usr/share/kosher/site-rules.json")
    filt.page_levels = {}
    filt.blocklist = search_mod.load_blocklist(
        root / "os-image/files/usr/share/kosher/search-blocklist.json")
    filt.scorer = content.load(
        root / "os-image/files/usr/share/kosher/content-terms.json")
    return filt


def test_a_department_search_on_the_shops_own_box_is_blocked(addon):
    filt = _shop_filter(addon)
    flow = _request_flow("https://www.amazon.com/s?k=lingerie")
    filt.request(flow)
    assert flow.response is not None and flow.response.status_code == 403


def test_an_ordinary_search_on_the_same_shop_is_not(addon):
    filt = _shop_filter(addon)
    flow = _request_flow("https://www.amazon.com/s?k=laptop+stand")
    filt.request(flow)
    assert flow.response is None


def test_autocomplete_entries_are_dropped_from_the_response(addon):
    import json

    filt = _shop_filter(addon)
    body = json.dumps({"prefix": "li", "suggestions": [
        {"value": "light bulbs"}, {"value": "lingerie"},
        {"value": "linen tablecloth"}]}).encode()
    flow = _suggestion_flow(
        "https://completion.amazon.com/api/2017/suggestions?prefix=li", body)
    asyncio.run(filt.response(flow))
    out = json.loads(flow.response.content)
    assert [s["value"] for s in out["suggestions"]] == ["light bulbs",
                                                        "linen tablecloth"]
    assert out["prefix"] == "li"


def test_a_stale_content_length_is_not_left_behind(addon):
    import json

    filt = _shop_filter(addon)
    body = json.dumps(["lamp", "lingerie"]).encode()
    flow = _suggestion_flow(
        "https://completion.amazon.com/api/2017/suggestions?prefix=l", body)
    flow.response.headers["content-length"] = str(len(body))
    asyncio.run(filt.response(flow))
    assert "content-length" not in flow.response.headers


def test_suggestions_are_left_alone_for_accounts_that_did_not_ask(addon):
    import json

    filt = _shop_filter(addon, blocked=("gambling",))
    body = json.dumps(["lamp", "lingerie"]).encode()
    flow = _suggestion_flow(
        "https://completion.amazon.com/api/2017/suggestions?prefix=l", body)
    asyncio.run(filt.response(flow))
    assert json.loads(flow.response.content) == ["lamp", "lingerie"]


def test_a_results_page_is_judged_even_when_pictures_are_not_filtered(addon):
    # An account that blocks immodest sites did not ask to read an
    # immodest page on a site it does not block.
    filt = _shop_filter(addon)
    flow = _suggestion_flow("https://www.amazon.com/s?k=x", b"")
    flow.response.headers["content-type"] = "text/html"
    flow.response.content = (
        "<html><body>Lingerie sale: bras, panties, thongs, bralettes, "
        "corsets and intimate apparel. Sexy babydoll and negligee sets."
        "</body></html>")
    filt._filter_page(flow, 1001)
    assert flow.response.status_code == 403


def _suggestion_flow(url, body):
    from urllib.parse import urlsplit

    parts = urlsplit(url)
    return type("F", (), {
        "request": type("R", (), {
            "pretty_host": parts.hostname,
            "pretty_url": url,
            "path": parts.path,
            "method": "GET",
            "query": {},
            "headers": {},
        })(),
        "response": _Resp(body, {"content-type": "application/json"}),
        "metadata": {"kosher_uid": 1001},
    })()


def test_a_shops_navigation_is_removed_rather_than_held_against_it(addon):
    # An Amazon search for socks lists Lingerie, Bras, Panties and
    # Swimwear in the sidebar of every page. Scoring that gives two bad
    # answers and no good one: strictly and Amazon is blocked outright,
    # loosely and the sidebar stays on screen. So the items go instead.
    filt = _shop_filter(addon)
    flow = _suggestion_flow("https://www.amazon.com/s?k=socks", b"")
    flow.response.headers["content-type"] = "text/html"
    nav = ("<html><body><ul id=\"depts\">"
           "<li><a href=\"/b/clothing\">Clothing, Shoes &amp; Jewelry</a></li>"
           "<li><a href=\"/b/womens-lingerie\">Lingerie, Sleepwear &amp; "
           "Loungewear</a></li>"
           "<li><a href=\"/b/bras\">Bras</a></li>"
           "<li><a href=\"/b/panties\">Panties</a></li>"
           "<li><a href=\"/b/swim\">Swimwear</a></li>"
           "<li><a href=\"/b/socks\">Socks &amp; Hosiery</a></li>"
           "</ul><p>Results for socks: cotton crew socks, wool socks."
           "</p></body></html>")
    flow.response.content = nav.encode()
    filt._filter_page(flow, 1001)

    assert getattr(flow.response, "status_code", 200) != 403
    served = flow.response.text
    for gone in ("Lingerie", "Bras", "Panties", "Swimwear"):
        assert gone not in served, gone
    # And the rest of the page is untouched, including its entities.
    assert "Socks &amp; Hosiery" in served
    assert "Clothing, Shoes &amp; Jewelry" in served
    assert "cotton crew socks" in served


def test_a_page_too_large_to_rewrite_falls_back_to_a_looser_floor(addon):
    # Nothing could be removed, so the catalogue vocabulary is still in
    # the text and would convict a page about socks.
    from kosherd import elementfilter

    filt = _shop_filter(addon)
    flow = _suggestion_flow("https://www.amazon.com/s?k=socks", b"")
    flow.response.headers["content-type"] = "text/html"
    filler = "<p>cotton crew socks</p>" * 100
    huge = ("<html><body><ul><li>Lingerie</li><li>Bras</li><li>Panties</li>"
            "<li>Swimwear</li></ul>" + filler
            + "x" * elementfilter.MAX_PAGE + "</body></html>")
    flow.response.content = huge.encode()
    filt._filter_page(flow, 1001)
    assert getattr(flow.response, "status_code", 200) != 403


def test_a_page_on_a_site_with_no_rules_is_still_judged_strictly(addon):
    # Nothing precise covers this host, so the scorer is all there is.
    filt = _shop_filter(addon)
    flow = _suggestion_flow("https://smallshop.example/page", b"")
    flow.response.headers["content-type"] = "text/html"
    flow.response.content = (
        "<html><body>Bikini and swimsuit collection. Tankini, monokini and "
        "beachwear for summer. Swimsuit edition photos.</body></html>")
    filt._filter_page(flow, 1001)
    assert flow.response.status_code == 403


# -- YouTube after the first page ---------------------------------------------

def test_youtube_hosts_are_matched_by_suffix_not_substring(addon):
    for host in ("www.youtube.com", "m.youtube.com", "music.youtube.com",
                 "www.youtube-nocookie.com", "youtubekids.com"):
        assert addon.YouTube.applies(host), host
    for host in ("youtube.com.attacker.example", "notyoutube.com",
                 "chinuch.org", "myyoutube.com"):
        assert not addon.YouTube.applies(host), host


def test_the_player_api_is_recognised(addon):
    # YouTube is a single-page app: after the first load every video comes
    # from here, and a filter that only reads watch pages checks the first
    # video a child opens and nothing they click afterwards.
    assert addon.YouTube.is_player_api("/youtubei/v1/player")
    assert addon.YouTube.is_player_api("/youtubei/v1/player?key=abc")
    assert addon.YouTube.is_player_api("/youtubei/v1/reel/reel_item_watch")
    assert not addon.YouTube.is_player_api("/youtubei/v1/search")
    assert not addon.YouTube.is_player_api("/watch")


def test_shorts_and_embeds_and_live_are_watch_paths(addon):
    for path in ("/watch", "/shorts", "/embed", "/live", "/v/"):
        assert path in addon.YouTube.WATCH_PATHS, path


def _player_flow(addon, body):
    return type("F", (), {
        "request": type("R", (), {
            "pretty_host": "www.youtube.com",
            "pretty_url": "https://www.youtube.com/youtubei/v1/player",
            "path": "/youtubei/v1/player",
            "method": "POST", "query": {}, "headers": {},
        })(),
        "response": _Resp(body, {"content-type": "application/json",
                                 "content-length": str(len(body))}),
        "metadata": {"kosher_uid": 1001},
    })()


def _yt_filter(addon, youtube):
    filt = addon.KosherFilter.__new__(addon.KosherFilter)
    filt.policy = type("P", (), {
        "youtube_for": staticmethod(lambda uid: youtube),
        "media_level_for": staticmethod(lambda uid: "none"),
        "language_filter_for": staticmethod(lambda uid: "off"),
        "blocked_categories_for": staticmethod(lambda uid: []),
    })()
    return filt


def test_a_blocked_category_is_stopped_in_the_player_api(addon):
    import json

    filt = _yt_filter(addon, {"blocked_categories": ["24"]})
    body = json.dumps({"videoDetails": {"channelId": "UC1"},
                       "microformat": {"category": "24"}})
    flow = _player_flow(addon, body)
    filt._filter_youtube(flow, 1001)
    answer = json.loads(flow.response.text)
    assert answer["playabilityStatus"]["status"] == "ERROR"
    assert "turned off" in answer["playabilityStatus"]["reason"]


def test_an_unapproved_channel_is_stopped_in_the_player_api(addon):
    import json

    filt = _yt_filter(addon, {"allowed_channels": ["@torah"]})
    flow = _player_flow(addon, json.dumps(
        {"videoDetails": {"channelId": "UCsomethingelse"}}))
    filt._filter_youtube(flow, 1001)
    assert json.loads(flow.response.text)["playabilityStatus"]["status"] == "ERROR"


def test_an_approved_channel_plays(addon):
    import json

    filt = _yt_filter(addon, {"allowed_channels": ["@torahchannel"]})
    body = json.dumps({"videoDetails": {"channelId": "UC1"},
                       "canonicalBaseUrl": "/@torahchannel"})
    flow = _player_flow(addon, body)
    filt._filter_youtube(flow, 1001)
    assert flow.response.content == body.encode() or flow.response.text == body


def test_the_answer_is_json_the_player_understands(addon):
    # A 403 here spins forever and an HTML block page is a broken app;
    # this response is consumed by the player, not read by a person.
    import json

    filt = _yt_filter(addon, {"blocked_categories": ["24"]})
    flow = _player_flow(addon, json.dumps({"microformat": {"category": "24"}}))
    filt._filter_youtube(flow, 1001)
    assert flow.response.headers["content-type"] == "application/json"
    assert flow.response.headers["x-kosheros"] == "youtube-blocked"
    # The body changed, so a stale length would be a lie the browser enforces.
    assert "content-length" not in flow.response.headers
    answer = json.loads(flow.response.text)
    assert "errorScreen" in answer["playabilityStatus"]
    assert answer["videoDetails"] == {}


def test_a_category_named_the_way_youtube_names_it_is_stopped(addon):
    # The settings keep ids ("10"); YouTube's player JSON says "Music".
    # Nothing ever matched, and every category limit was a no-op.
    import json

    filt = _yt_filter(addon, {"blocked_categories": ["24", "20"]})
    body = json.dumps({"videoDetails": {"channelId": "UC1"},
                       "microformat": {"playerMicroformatRenderer": {
                           "category": "Entertainment", "title": {"simpleText": "x"}}}})
    flow = _player_flow(addon, body)
    filt._filter_youtube(flow, 1001)
    assert json.loads(flow.response.text)["playabilityStatus"]["status"] == "ERROR"

    allowed = _player_flow(addon, json.dumps({"microformat": {
        "playerMicroformatRenderer": {"category": "Music"}}}))
    filt._filter_youtube(allowed, 1001)
    assert "playabilityStatus" not in allowed.response.text, "Music is allowed"


def test_category_names_map_to_ids_however_youtube_spells_them(addon):
    assert addon.YouTube.category_of('{"category":"Music"}') == "10"
    assert addon.YouTube.category_of('{"category":"Howto & Style"}') == "26"
    assert addon.YouTube.category_of('{"category":"24"}') == "24"
    assert addon.YouTube.category_of('{"category":"Not A Category"}') is None
    # The microformat's category wins over another "category" key earlier
    # in the body.
    body = ('{"adPlacements":{"category":"Gaming"},'
            '"microformat":{"playerMicroformatRenderer":{"category":"Education"}}}')
    assert addon.YouTube.category_of(body) == "27"


def test_the_microformats_category_is_found_after_a_long_description(addon):
    # On YouTube's real player JSON the renderer's category sits about nine
    # thousand characters after the renderer opens (thumbnails, embed HTML,
    # the description). An earlier "category" key elsewhere must still lose
    # to it.
    import json

    long_description = "x" * 9000
    body = json.dumps({"adPlacements": {"category": "Gaming"},
                       "microformat": {"playerMicroformatRenderer": {
                           "description": {"simpleText": long_description},
                           "category": "Music"}}})
    assert addon.YouTube.category_of(body) == "10"


def test_shorts_are_refused_everywhere_they_play_when_turned_off(addon):
    import json

    filt = _yt_filter(addon, {"blocked_categories": ["shorts"]})
    # The reel player.
    reel = _player_flow(addon, json.dumps({"microformat": {"category": "Music"}}))
    reel.request.path = "/youtubei/v1/reel/reel_item_watch"
    filt._filter_youtube(reel, 1001)
    assert json.loads(reel.response.text)["playabilityStatus"]["status"] == "ERROR"
    assert "Shorts" in json.loads(reel.response.text)["playabilityStatus"]["reason"]
    # The ordinary player, asked from a /shorts page.
    body = json.dumps({"microformat": {"playerMicroformatRenderer": {"category": "Music"}}})
    short = _player_flow(addon, body)
    short.request.headers = {"referer": "https://www.youtube.com/shorts/abc123"}
    filt._filter_youtube(short, 1001)
    assert json.loads(short.response.text)["playabilityStatus"]["status"] == "ERROR"
    # The same request from a watch page plays: Music is not blocked.
    watch = _player_flow(addon, body)
    watch.request.headers = {"referer": "https://www.youtube.com/watch?v=abc123"}
    filt._filter_youtube(watch, 1001)
    assert "playabilityStatus" not in watch.response.text


def test_the_shorts_page_itself_is_blocked_when_turned_off(addon):
    filt = _yt_filter(addon, {"blocked_categories": ["shorts"]})
    flow = _player_flow(addon, "<html>short</html>")
    flow.request.path = "/shorts/abc123"
    flow.request.pretty_url = "https://www.youtube.com/shorts/abc123"
    flow.response.headers["content-type"] = "text/html"
    filt._filter_youtube(flow, 1001)
    assert flow.response.status_code == 403
    assert b"Shorts are turned off" in flow.response.content


def test_shorts_shelves_and_reel_links_are_pruned_from_the_feeds(addon):
    import json

    filt = _yt_filter(addon, {"blocked_categories": ["shorts"]})
    feed = {"contents": [
        {"videoRenderer": {"videoId": "v1", "title": {"runs": [{"text": "A shiur"}]}}},
        {"reelShelfRenderer": {"items": [{"reelItemRenderer": {"videoId": "s1"}}]}},
        {"richItemRenderer": {"content": {"shortsLockupViewModel": {"entityId": "s2"}}}},
        {"videoRenderer": {"videoId": "v2", "navigationEndpoint": {
            "reelWatchEndpoint": {"videoId": "v2"}}}},
        {"videoRenderer": {"videoId": "v3"}},
    ]}
    flow = _player_flow(addon, json.dumps(feed))
    flow.request.path = "/youtubei/v1/browse"
    filt._filter_youtube(flow, 1001)
    kept = json.loads(flow.response.text)["contents"]
    assert [next(iter(item.values())).get("videoId") for item in kept] == ["v1", "v3"]
    assert flow.response.headers["x-kosheros"] == "youtube-feed-filtered"


def test_youtube_thumbnails_count_as_search_thumbnails(addon):
    assert addon._is_search_thumb("i.ytimg.com")
    assert addon._is_search_thumb("i9.ytimg.com")
    assert not addon._is_search_thumb("www.youtube.com")


def test_a_video_whose_kind_cannot_be_read_does_not_play_where_kinds_are_limited(addon):
    # The player JSON always names a category; when it does not, the safe
    # reading for an account that limits kinds is that this one is hidden.
    import json

    filt = _yt_filter(addon, {"blocked_categories": ["24"]})
    flow = _player_flow(addon, json.dumps({"videoDetails": {"channelId": "UC1"}}))
    filt._filter_youtube(flow, 1001)
    assert json.loads(flow.response.text)["playabilityStatus"]["status"] == "ERROR"
    # Only Shorts turned off is not a kind limit: an ordinary video with no
    # readable category still plays.
    filt = _yt_filter(addon, {"blocked_categories": ["shorts"]})
    flow = _player_flow(addon, json.dumps({"videoDetails": {"channelId": "UC1"}}))
    filt._filter_youtube(flow, 1001)
    assert "playabilityStatus" not in flow.response.text
    # And a watch PAGE without a readable category is left to the player
    # call that follows it, rather than blocked on the doubt.
    assert addon.KosherFilter._youtube_verdict("<html></html>", [], ["24"]) is None


def test_the_journal_says_why_a_youtube_video_played(addon, caplog):
    # "youtube still not blocking" is impossible to chase without knowing
    # whether the proxy saw the video and what it made of it. Three lines
    # answer it: no limits reached the proxy, no body to read, or the kind
    # it read and the kinds turned off.
    import json
    import logging

    caplog.set_level(logging.INFO)
    filt = _yt_filter(addon, {})
    filt._filter_youtube(_player_flow(addon, "{}"), 1001)
    assert "no YouTube limits are set for uid=1001" in caplog.text

    caplog.clear()
    filt = _yt_filter(addon, {"blocked_categories": ["24"]})
    body = json.dumps({"microformat": {"playerMicroformatRenderer": {"category": "Music"}}})
    filt._filter_youtube(_player_flow(addon, body), 1001)
    assert "its kind is 10" in caplog.text and "'24'" in caplog.text

    caplog.clear()
    flow = _player_flow(addon, "")
    filt._filter_youtube(flow, 1001)
    assert "had no body to read" in caplog.text


def test_an_account_with_no_youtube_limits_is_left_alone(addon):
    import json

    filt = _yt_filter(addon, {"restrict": "strict"})
    body = json.dumps({"microformat": {"category": "24"}})
    flow = _player_flow(addon, body)
    filt._filter_youtube(flow, 1001)
    assert flow.response.text == body


# -- the feeds a child looks at -----------------------------------------------

def _renderer(channel_title, browse_id=None, handle=None, title="A video"):
    run = {"text": channel_title}
    endpoint = {}
    if browse_id:
        endpoint["browseId"] = browse_id
    if handle:
        endpoint["canonicalBaseUrl"] = f"/{handle}"
    if endpoint:
        run["navigationEndpoint"] = {"browseEndpoint": endpoint}
    return {"videoRenderer": {"videoId": "x", "title": {"runs": [{"text": title}]},
                              "ownerText": {"runs": [run]}}}


def test_a_feed_keeps_only_approved_channels(addon):
    feed = {"contents": [_renderer("Torah Channel", handle="@torah"),
                         _renderer("Something Else", handle="@other"),
                         _renderer("Torah Channel", handle="@torah")]}
    pruned = addon.YouTube.prune_feed(feed, {"@torah"})
    assert len(pruned["contents"]) == 2


def test_a_channel_may_be_named_by_id_or_handle_or_title(addon):
    feed = {"contents": [_renderer("Torah Channel", browse_id="UC123")]}
    for allowed in ({"UC123"}, {"@torah"}, {"Torah Channel"}):
        item = _renderer("Torah Channel", browse_id="UC123", handle="@torah")
        assert addon.YouTube.prune_feed({"contents": [item]}, allowed)["contents"]
    # ...and something that names none of them goes.
    assert addon.YouTube.prune_feed(feed, {"@torah"})["contents"] == []


def test_a_title_mentioning_an_approved_channel_is_not_that_channel(addon):
    # Matched by shape, not by searching the text.
    item = _renderer("Some Other Channel", handle="@other",
                     title="A reply to Torah Channel")
    assert addon.YouTube.prune_feed({"contents": [item]},
                                    {"Torah Channel"})["contents"] == []


def test_anything_that_does_not_name_a_channel_is_left_alone(addon):
    # A filter that guesses at an app's internals breaks it, so an
    # unfamiliar shape survives untouched.
    feed = {"header": {"someRenderer": {"title": "Home"}},
            "contents": [{"messageRenderer": {"text": "Nothing here"}},
                         {"continuationItemRenderer": {"token": "abc"}}]}
    assert addon.YouTube.prune_feed(feed, {"@torah"}) == feed


def test_pruning_is_bounded_on_deeply_nested_json(addon):
    node = {"contents": []}
    for _ in range(200):
        node = {"contents": [node]}
    addon.YouTube.prune_feed(node, {"@torah"})  # must return, not recurse away


def test_the_feed_is_only_pruned_for_approved_channel_accounts(addon):
    import json

    body = json.dumps({"contents": [_renderer("Anything", handle="@any")]})
    for youtube in ({"blocked_categories": ["24"]}, {"restrict": "strict"}):
        filt = _yt_filter(addon, youtube)
        flow = _feed_flow(body)
        filt._filter_youtube(flow, 1001)
        assert flow.response.text == body, youtube


def test_a_pruned_feed_drops_its_stale_length(addon):
    import json

    filt = _yt_filter(addon, {"allowed_channels": ["@torah"]})
    body = json.dumps({"contents": [_renderer("Other", handle="@other")]})
    flow = _feed_flow(body)
    filt._filter_youtube(flow, 1001)
    assert flow.response.headers["x-kosheros"] == "youtube-feed-filtered"
    assert "content-length" not in flow.response.headers
    assert json.loads(flow.response.text)["contents"] == []


def test_a_feed_that_is_not_json_is_left_alone(addon):
    filt = _yt_filter(addon, {"allowed_channels": ["@torah"]})
    flow = _feed_flow("<html>not json</html>")
    filt._filter_youtube(flow, 1001)
    assert flow.response.text == "<html>not json</html>"


def _feed_flow(body):
    if isinstance(body, str):
        body = body.encode()
    return type("F", (), {
        "request": type("R", (), {
            "pretty_host": "www.youtube.com",
            "pretty_url": "https://www.youtube.com/youtubei/v1/browse",
            "path": "/youtubei/v1/browse",
            "method": "POST", "query": {}, "headers": {},
        })(),
        "response": _Resp(body, {"content-type": "application/json",
                                 "content-length": str(len(body))}),
        "metadata": {"kosher_uid": 1001},
    })()


def test_the_proxy_hides_a_person_on_a_page_that_reads_as_immodest(addon):
    from kosherd import vision

    filt = addon.KosherFilter.__new__(addon.KosherFilter)
    filt.policy = _stub_policy(media="immodest")
    filt.categories = _stub_categories()
    filt.page_levels = {"https://shop.example/lingerie": vision.IMMODEST}
    # A clothed model: no exposure labels, so the detector says clean.
    filt.vision = _stub_vision(vision.ImageVerdict(vision.CLEAN, (),
                                                   has_person=True))
    flow = _image_flow()
    flow.request.headers["referer"] = "https://shop.example/lingerie"
    filt._filter_image(flow, 1001)
    assert flow.response.content == addon.BLANK_PNG


def test_the_shops_logo_on_the_same_page_survives(addon):
    from kosherd import vision

    filt = addon.KosherFilter.__new__(addon.KosherFilter)
    filt.policy = _stub_policy(media="immodest")
    filt.categories = _stub_categories()
    filt.page_levels = {"https://shop.example/lingerie": vision.IMMODEST}
    filt.vision = _stub_vision(vision.ImageVerdict(vision.CLEAN, (),
                                                   has_person=False))
    flow = _image_flow()
    flow.request.headers["referer"] = "https://shop.example/lingerie"
    original = flow.response.content
    filt._filter_image(flow, 1001)
    assert flow.response.content == original


def test_a_page_the_proxy_never_judged_hides_nothing_extra(addon):
    from kosherd import vision

    filt = addon.KosherFilter.__new__(addon.KosherFilter)
    filt.policy = _stub_policy(media="immodest")
    filt.categories = _stub_categories()
    filt.page_levels = {}
    filt.vision = _stub_vision(vision.ImageVerdict(vision.CLEAN, (),
                                                   has_person=True))
    flow = _image_flow()
    flow.request.headers["referer"] = "https://news.example/story"
    original = flow.response.content
    filt._filter_image(flow, 1001)
    assert flow.response.content == original


def test_remembered_pages_do_not_grow_without_bound(addon):
    filt = addon.KosherFilter.__new__(addon.KosherFilter)
    filt.page_levels = {}
    from kosherd import vision

    for i in range(filt.MAX_REMEMBERED_PAGES * 3):
        filt._remember_page(f"https://example.com/{i}", vision.IMMODEST)
    assert len(filt.page_levels) <= filt.MAX_REMEMBERED_PAGES


def test_a_clean_page_is_not_remembered_at_all(addon):
    from kosherd import content, vision

    filt = addon.KosherFilter.__new__(addon.KosherFilter)
    filt.page_levels = {"https://example.com/x": vision.IMMODEST}
    filt._remember_page("https://example.com/x", content.CLEAN)
    assert filt.page_levels == {}


# -- per-user listener ports -------------------------------------------------

def test_the_cache_maps_listener_ports_to_users(addon, tmp_path):
    # kosherd redirects each filtered user to their own port; the proxy
    # learns who is connecting from the port alone. Nothing to race.
    path = _rules_file(tmp_path, {
        "1000": {"port": 30000, "rules": [], "blocked_categories": ["adult"]},
        "1001": {"port": 30001, "rules": [], "blocked_categories": ["social"]},
    })
    cache = addon.PolicyCache(path)
    assert cache.uid_for_listener_port(30000) == 1000
    assert cache.uid_for_listener_port(30001) == 1001
    assert cache.uid_for_listener_port(8080) is None
    assert cache.uid_for_listener_port(None) is None


def _mode(port: int):
    # Mimics mitmproxy's ProxyMode: the listener the connection arrived on.
    return types.SimpleNamespace(custom_listen_port=port,
                                 full_spec=f"transparent@127.0.0.1:{port}")


def test_the_filter_identifies_a_user_by_the_listener_port(addon, tmp_path):
    path = _rules_file(tmp_path, {"1001": {"port": 30001, "rules": []}})
    filt = addon.KosherFilter.__new__(addon.KosherFilter)
    filt.policy = addon.PolicyCache(path)
    # A socket-table lookup that would say "root" must NEVER be consulted.
    filt.uids = type("U", (), {"uid_for_connection": staticmethod(lambda *a, **k: 0)})()
    # In transparent mode sockname is the ORIGINAL DESTINATION, not the
    # listener — the trap the old code fell into. Set it to prove the port
    # comes from proxy_mode instead.
    flow = types.SimpleNamespace(
        client_conn=types.SimpleNamespace(
            proxy_mode=_mode(30001),
            sockname=("93.184.216.34", 443),  # the real server, a red herring
            peername=("10.0.2.15", 51515)),
        server_conn=types.SimpleNamespace(address=("93.184.216.34", 443)))
    assert filt._uid_of(flow) == 1001


def test_the_listener_port_is_read_from_the_proxy_mode(addon):
    flow = types.SimpleNamespace(client_conn=types.SimpleNamespace(proxy_mode=_mode(30005)))
    assert addon.KosherFilter._listener_port(flow) == 30005
    # Fallback to the spec string if the attribute is ever missing.
    flow.client_conn.proxy_mode = types.SimpleNamespace(
        custom_listen_port=None, full_spec="transparent@127.0.0.1:30007")
    assert addon.KosherFilter._listener_port(flow) == 30007


def test_an_unresolvable_port_falls_back_to_the_socket_scan(addon, tmp_path):
    # No proxy_mode (some future mode, or a non-transparent path): the
    # 4-tuple socket lookup is the fallback, not a crash.
    path = _rules_file(tmp_path, {"1001": {"port": 30001, "rules": []}})
    filt = addon.KosherFilter.__new__(addon.KosherFilter)
    filt.policy = addon.PolicyCache(path)
    filt.uids = type("U", (), {"uid_for_connection": staticmethod(lambda *a, **k: 1001)})()
    flow = types.SimpleNamespace(
        client_conn=types.SimpleNamespace(proxy_mode=None,
                                          peername=("10.0.2.15", 51515)),
        server_conn=types.SimpleNamespace(address=("1.2.3.4", 443)))
    assert filt._uid_of(flow) == 1001


# -- the hands-on media round: block-all means all; sources the catalogue
# -- already distrusts lose their imagery outright ----------------------------

def _image_flow_sized(addon, n_bytes, host="cdn.example.com", referer=""):
    headers = {"content-type": "image/jpeg"}
    req_headers = {"referer": referer} if referer else {}
    return types.SimpleNamespace(
        request=types.SimpleNamespace(pretty_host=host, pretty_url=f"https://{host}/x.jpg",
                                      headers=req_headers),
        response=types.SimpleNamespace(content=b"x" * n_bytes, headers=headers))


def _media_filter(addon, level, source_cats=()):
    filt = addon.KosherFilter.__new__(addon.KosherFilter)
    filt.policy = type("P", (), {
        "media_level_for": staticmethod(lambda uid: level),
        "blocked_categories_for": staticmethod(lambda uid: []),
    })()
    filt.categories = _stub_categories(source_cats)
    filt.page_levels = {}
    filt.vision = _stub_vision(None)
    return filt


def test_block_all_hides_even_tiny_images(addon):
    # Shopping thumbnails fit under any byte floor; "all" must mean all.
    filt = _media_filter(addon, "all")
    flow = _image_flow_sized(addon, 900)
    filt._filter_image(flow, 1001)
    assert flow.response.content == addon.BLANK_PNG


def test_an_immodest_source_loses_its_imagery_outright(addon):
    # people.com is catalogued immodest; a modesty-filtering account gets
    # its pictures hidden with no model involved.
    filt = _media_filter(addon, "immodest", source_cats={"immodest"})
    flow = _image_flow_sized(addon, 20000, host="people.com")
    filt._filter_image(flow, 1001)
    assert flow.response.content == addon.BLANK_PNG


def test_the_referring_page_counts_as_the_source_too(addon):
    # Celebrity sites serve their pictures from CDNs; the Referer names
    # the page the picture sits on.
    calls = []

    class C:
        @staticmethod
        def blocked_categories_of(host, blocked):
            return set()

        @staticmethod
        def categories_of(host):
            calls.append(host)
            return {"immodest"} if host == "people.com" else set()

    filt = _media_filter(addon, "immodest")
    filt.categories = C()
    flow = _image_flow_sized(addon, 20000, host="img.cdn.net",
                             referer="https://people.com/gallery")
    filt._filter_image(flow, 1001)
    assert flow.response.content == addon.BLANK_PNG
    assert "people.com" in calls


def test_the_weakest_level_only_distrusts_adult_sources(addon):
    from kosherd import vision

    filt = _media_filter(addon, "nsfw", source_cats={"immodest"})
    filt.vision = _stub_vision(vision.ImageVerdict(vision.CLEAN, ()))
    flow = _image_flow_sized(addon, 20000, host="people.com")
    filt._filter_image(flow, 1001)
    assert flow.response.content != addon.BLANK_PNG, \
        "nsfw-level accounts did not ask for celebrity sites to be hidden"


def test_inline_data_images_are_stripped_at_block_all(addon):
    import re as _re

    html = ('<html><body><img src="data:image/webp;base64,AAAA%s">' 
            '<p>hello</p></body></html>' % ("QUFB" * 200))
    blanked = addon.DATA_IMAGE_RE.sub(addon.BLANK_DATA_URI, html)
    assert "data:image/webp" not in blanked
    assert addon.BLANK_DATA_URI in blanked
    assert "<p>hello</p>" in blanked


def test_a_person_in_a_search_thumbnail_is_hidden_at_modesty_levels(addon):
    # Search thumbnails are the whole web shrunk past what the detector can
    # judge; beach and sheer-fabric shots sailed through at "immodest".
    # A detected person in one is now reason enough.
    from kosherd import vision

    filt = addon.KosherFilter.__new__(addon.KosherFilter)
    filt.policy = _stub_policy(media="immodest")
    filt.categories = _stub_categories()
    filt.page_levels = {}
    filt.vision = _stub_vision(vision.ImageVerdict(vision.CLEAN, (),
                                                   has_person=True))
    flow = _image_flow()
    flow.request.pretty_host = "tse1.mm.bing.net"
    filt._filter_image(flow, 1001)
    assert flow.response.content == addon.BLANK_PNG


def test_a_search_thumbnail_without_a_person_passes(addon):
    from kosherd import vision

    filt = addon.KosherFilter.__new__(addon.KosherFilter)
    filt.policy = _stub_policy(media="immodest")
    filt.categories = _stub_categories()
    filt.page_levels = {}
    filt.vision = _stub_vision(vision.ImageVerdict(vision.CLEAN, (),
                                                   has_person=False))
    flow = _image_flow()
    flow.request.pretty_host = "tse1.mm.bing.net"
    original = flow.response.content
    filt._filter_image(flow, 1001)
    assert flow.response.content == original, "a landscape thumbnail passes"


def test_search_thumb_hosts_are_recognised(addon):
    for host in ("tse1.mm.bing.net", "ts3.mm.bing.net",
                 "encrypted-tbn0.gstatic.com",
                 "external-content.duckduckgo.com"):
        assert addon._is_search_thumb(host), host
    for host in ("www.gstatic.com", "example.com", "images.example.com"):
        assert not addon._is_search_thumb(host), host


def test_an_allow_rule_beats_the_content_scorer(addon):
    # The administrator allowed a wrongly-blocked bookstore by URL rule and
    # it stayed blocked: rules were only honoured in the request hook, and
    # the response-side content scorer blocked it again a moment later.
    from kosherd import content

    filt = addon.KosherFilter.__new__(addon.KosherFilter)
    filt.policy = type("P", (), {
        "media_level_for": staticmethod(lambda uid: "immodest"),
        "language_filter_for": staticmethod(lambda uid: "off"),
        "blocked_categories_for": staticmethod(lambda uid: []),
        "rules_for": staticmethod(lambda uid: addon.parse_rules(
            [{"action": "allow", "pattern": "books.example/*"}])),
    })()
    filt.categories = _stub_categories()
    filt.page_levels = {}
    filt.siterules = type("S", (), {
        "reason": staticmethod(lambda url: None),
        "is_suggestions": staticmethod(lambda url: False)})()
    filt.scorer = content.Scorer({"erotica": (content.NSFW, 25),
                                  "xxx": (content.NSFW, 25)})
    filt.wordlist = type("W", (), {
        "contains_any": staticmethod(lambda text: False)})()

    body = "<html><body>erotica xxx erotica xxx</body></html>"
    flow = types.SimpleNamespace(
        request=types.SimpleNamespace(
            pretty_host="books.example",
            pretty_url="https://books.example/genres", headers={}),
        response=types.SimpleNamespace(
            headers={"content-type": "text/html"},
            get_text=lambda strict=False: body))
    original = flow.response
    filt._filter_page(flow, 1001)
    # _block replaces the response object; the allow rule must prevent that.
    assert flow.response is original, \
        "an explicit allow rule must beat the content scorer"

    # And the same page WITHOUT the rule is blocked — proving the scorer
    # genuinely convicts it and the rule is what saved it.
    filt.policy.rules_for = staticmethod(lambda uid: [])
    flow2 = types.SimpleNamespace(
        request=types.SimpleNamespace(
            pretty_host="books.example",
            pretty_url="https://books.example/genres", headers={}),
        response=types.SimpleNamespace(
            headers={"content-type": "text/html"},
            get_text=lambda strict=False: body))
    filt._remember_page = lambda url, level: None
    filt._block = lambda *a, **k: blocked.append(True)
    blocked = []
    filt._filter_page(flow2, 1001)
    assert blocked, "without the rule the scorer must convict this page"


# -- holding only what must be held ------------------------------------------

def _hflow(content_type, uid=1001, length=None, url="https://example.com/a", status=200,
           content_range=None, body=b"\x00" * 100):
    headers = {"content-type": content_type}
    if length is not None:
        headers["content-length"] = str(length)
    if content_range:
        headers["content-range"] = content_range
    from urllib.parse import urlsplit

    resp = _Resp(body, headers)
    resp.status_code = status
    return types.SimpleNamespace(
        metadata={"kosher_uid": uid},
        request=types.SimpleNamespace(pretty_host=urlsplit(url).hostname, pretty_url=url,
                                      headers={}),
        response=resp)


def _stub_videos(available=True, cached=None, verdict=None):
    async def verdict_async(data, key):
        return verdict

    return type("VC", (), {
        "available": available,
        "cached": staticmethod(lambda key: cached),
        "verdict": staticmethod(lambda data, key: verdict),
        "verdict_async": staticmethod(verdict_async),
    })()


def _filt(addon, media="immodest", blocked=(), videos=None, vision=None):
    filt = addon.KosherFilter.__new__(addon.KosherFilter)
    filt.policy = _stub_policy(media=media, blocked=blocked)
    filt.categories = _stub_categories()
    filt.page_levels = {}
    filt.siterules = type("S", (), {"is_suggestions": staticmethod(lambda url: False)})()
    filt.videos = videos
    filt.vision = vision
    filt.covers = addon.imageedit_mod.CoverCache()
    return filt


def test_downloads_stream_through_instead_of_being_held(addon):
    filt = _filt(addon)
    for kind in ("application/octet-stream", "application/zip", "audio/mpeg", "font/woff2"):
        flow = _hflow(kind)
        filt.responseheaders(flow)
        assert flow.response.stream is True, kind


def test_a_large_unknown_body_streams_but_a_page_never_does(addon):
    filt = _filt(addon)
    big = _hflow("application/x-something", length=50_000_000)
    filt.responseheaders(big)
    assert big.response.stream is True
    page = _hflow("text/html", length=50_000_000)
    filt.responseheaders(page)
    assert not getattr(page.response, "stream", False), "pages are read, so held"


def test_pictures_are_held_only_when_they_will_be_looked_at(addon):
    held = _hflow("image/jpeg")
    _filt(addon, media="immodest").responseheaders(held)
    assert not getattr(held.response, "stream", False)
    passed = _hflow("image/jpeg")
    _filt(addon, media="none").responseheaders(passed)
    assert passed.response.stream is True


def _real_image_flow(addon, width=640, height=480, url="https://cdn.example.com/p.jpg"):
    import io

    from PIL import Image

    out = io.BytesIO()
    Image.new("RGB", (width, height), (90, 120, 200)).save(out, "JPEG")
    return types.SimpleNamespace(
        request=types.SimpleNamespace(pretty_host="cdn.example.com", pretty_url=url,
                                      headers={}),
        response=types.SimpleNamespace(content=out.getvalue(),
                                       headers={"content-type": "image/jpeg"}),
        metadata={"kosher_uid": 1001})


def test_a_hidden_picture_keeps_its_size_so_the_page_does_not_jump(addon):
    import io

    from PIL import Image

    filt = _filt(addon, media="immodest")
    flow = _real_image_flow(addon, 320, 200)
    filt._blank_image(flow)
    with Image.open(io.BytesIO(flow.response.content)) as tile:
        assert tile.size == (320, 200)
        assert tile.getpixel((5, 5)) == addon.imageedit_mod.PLACEHOLDER_GREY
    assert flow.response.headers["content-type"] == "image/png"
    assert flow.response.headers["x-kosheros"] == "image-hidden"
    # A picture whose size cannot be read still gets the old one-pixel blank.
    odd = _image_flow_sized(addon, 4000)
    odd.metadata = {"kosher_uid": 1001}
    filt._blank_image(odd)
    assert odd.response.content == addon.BLANK_PNG


def test_a_picture_judged_clean_a_moment_ago_streams_instead_of_being_held(addon):
    from kosherd import vision

    filt = _filt(addon, media="immodest",
                 vision=_stub_vision(vision.ImageVerdict(vision.CLEAN, ())))
    first = _real_image_flow(addon)
    filt._filter_image(first, 1001)                   # judged clean by content
    again = _hflow("image/jpeg")
    again.request.pretty_url = "https://cdn.example.com/p.jpg"
    filt.responseheaders(again)
    assert again.response.stream is True, "the same URL is not held twice"
    # A different URL, or a different media level, is still held.
    other = _hflow("image/jpeg")
    other.request.pretty_url = "https://cdn.example.com/q.jpg"
    filt.responseheaders(other)
    assert not getattr(other.response, "stream", False)
    stricter = _filt(addon, media="suggestive")
    stricter.__dict__["_clean_urls"] = filt.__dict__["_clean_urls"]
    same = _hflow("image/jpeg")
    same.request.pretty_url = "https://cdn.example.com/p.jpg"
    stricter.responseheaders(same)
    assert not getattr(same.response, "stream", False)


def test_a_hidden_or_covered_picture_is_never_remembered_as_clean(addon):
    from kosherd import vision

    verdict = vision.ImageVerdict(vision.IMMODEST, ((10, 10, 50, 50),), True)
    filt = _filt(addon, media="immodest", vision=_stub_vision(verdict))
    flow = _real_image_flow(addon)
    filt._filter_image(flow, 1001)
    assert not filt.__dict__.get("_clean_urls")
    again = _hflow("image/jpeg")
    again.request.pretty_url = "https://cdn.example.com/p.jpg"
    filt.responseheaders(again)
    assert not getattr(again.response, "stream", False)


def test_the_clean_memo_forgets_after_its_window(addon, monkeypatch):
    filt = _filt(addon, media="immodest")
    flow = _real_image_flow(addon)
    filt._remember_clean(flow, "immodest")
    later = _hflow("image/jpeg")
    later.request.pretty_url = "https://cdn.example.com/p.jpg"
    import time as _time

    real = _time.monotonic
    monkeypatch.setattr(addon.time, "monotonic", lambda: real() + filt.CLEAN_URL_SECONDS + 1)
    assert filt._recently_clean(later, "immodest") is False


def test_a_connection_that_is_nobodys_streams(addon):
    flow = _hflow("text/html", uid=None)
    _filt(addon).responseheaders(flow)
    assert flow.response.stream is True


def test_video_streams_at_the_level_that_does_not_filter_pictures(addon):
    flow = _hflow("video/mp4", length=1000)
    _filt(addon, media="none").responseheaders(flow)
    assert flow.response.stream is True


def test_video_is_refused_before_a_byte_is_fetched_when_it_cannot_be_checked(addon):
    # No decoder on this machine: an account whose pictures are filtered
    # gets no open-web video, as before — and does not download it first.
    flow = _hflow("video/mp4", length=1000)
    _filt(addon, media="immodest", videos=_stub_videos(available=False)).responseheaders(flow)
    assert flow.response.status_code == 403
    assert flow.response.headers["x-kosheros"] == "video-blocked"
    assert callable(flow.response.stream), "the body is discarded as it arrives"
    assert flow.response.stream(b"chunk") == b""


def test_the_mildest_level_keeps_passing_video_it_cannot_check(addon):
    # Video passed unchecked at "nsfw" before frame sampling existed; a
    # missing decoder must not take that away.
    flow = _hflow("video/mp4", length=1000)
    _filt(addon, media="nsfw", videos=_stub_videos(available=False)).responseheaders(flow)
    assert flow.response.stream is True


def test_a_checkable_clip_is_held_for_sampling(addon):
    flow = _hflow("video/mp4", length=5_000_000)
    _filt(addon, media="immodest", videos=_stub_videos()).responseheaders(flow)
    assert not getattr(flow.response, "stream", False)


def test_a_clip_too_big_to_hold_is_refused_at_the_modesty_levels(addon):
    flow = _hflow("video/mp4", length=addon.videocheck_mod.VIDEO_MAX_BYTES + 1)
    _filt(addon, media="suggestive", videos=_stub_videos()).responseheaders(flow)
    assert flow.response.status_code == 403


def test_a_clip_already_judged_clean_streams_without_being_held(addon):
    from kosherd.vision import CLEAN, NSFW, ImageVerdict

    flow = _hflow("video/mp4", length=5_000_000)
    _filt(addon, media="immodest",
          videos=_stub_videos(cached=ImageVerdict(CLEAN, ()))).responseheaders(flow)
    assert flow.response.stream is True
    bad = _hflow("video/mp4", length=5_000_000)
    _filt(addon, media="immodest",
          videos=_stub_videos(cached=ImageVerdict(NSFW, ()))).responseheaders(bad)
    assert bad.response.status_code == 403


def test_a_range_from_the_middle_of_an_unjudged_clip_is_held_and_tried(addon):
    # Firefox asks for a clip in pieces and cancels its first request as
    # soon as it has the header; refusing every later piece outright meant
    # "breaking and stuck". The piece is held and decoded if it can be.
    flow = _hflow("video/mp4", status=206, content_range="bytes 500000-999999/2000000")
    _filt(addon, media="immodest", videos=_stub_videos()).responseheaders(flow)
    assert not getattr(flow.response, "stream", False)
    assert flow.response.status_code != 403


def test_a_clip_that_says_it_is_a_download_is_still_a_clip(addon):
    # CDNs serve .mp4 and .gif as application/octet-stream; keyed on the
    # content type alone the filter streamed them through unchecked.
    clip = _hflow("application/octet-stream", length=5_000_000,
                  url="https://cdn.example.com/media/clip.mp4?x=1")
    _filt(addon, media="immodest", videos=_stub_videos()).responseheaders(clip)
    assert not getattr(clip.response, "stream", False), "held for sampling"
    gif = _hflow("application/octet-stream", length=50_000,
                 url="https://cdn.example.com/media/fun.gif")
    assert addon._is_image(gif) and not addon._is_video(gif)
    other = _hflow("application/octet-stream", length=50_000,
                   url="https://cdn.example.com/files/setup.bin")
    assert not addon._is_image(other) and not addon._is_video(other)


def test_a_manifest_passes_where_its_segments_will_be_judged(addon):
    flow = _hflow("application/vnd.apple.mpegurl", url="https://example.com/live.m3u8")
    filt = _filt(addon, media="immodest", videos=_stub_videos())
    filt.responseheaders(flow)
    assert not getattr(flow.response, "stream", False)
    before = flow.response
    filt._filter_video(flow, 1001)
    assert flow.response is before, "segments are judged, not the text that lists them"
    # And where no video is allowed at all, neither is the manifest.
    none = _hflow("application/vnd.apple.mpegurl", url="https://example.com/live.m3u8")
    _filt(addon, media="all", videos=_stub_videos())._filter_video(none, 1001)
    assert none.response.status_code == 403


def test_a_held_clip_is_refused_or_passed_on_its_frames(addon):
    from kosherd.vision import CLEAN, IMMODEST, ImageVerdict

    clean = _hflow("video/mp4", length=1000)
    filt = _filt(addon, media="immodest", videos=_stub_videos(verdict=ImageVerdict(CLEAN, ())))
    filt._filter_video(clean, 1001)
    assert clean.response.headers["x-kosheros"] == "video-checked=clean"
    assert clean.response.status_code == 200

    bad = _hflow("video/mp4", length=1000)
    filt = _filt(addon, media="immodest",
                 videos=_stub_videos(verdict=ImageVerdict(IMMODEST, (), True)))
    filt._filter_video(bad, 1001)
    assert bad.response.status_code == 403

    unjudged = _hflow("video/mp4", length=1000)
    filt = _filt(addon, media="immodest", videos=_stub_videos(verdict=None))
    filt._filter_video(unjudged, 1001)
    assert unjudged.response.status_code == 403, "could not look: refuse where unchecked video is"
    # The mildest level passes what it cannot check, from the body as from
    # the headers; a clip is not passed by one and refused by the other.
    mild = _hflow("video/mp4", length=1000)
    filt = _filt(addon, media="nsfw", videos=_stub_videos(verdict=None))
    filt._filter_video(mild, 1001)
    assert mild.response.status_code == 200
    assert mild.response.headers["x-kosheros"] == "video-unchecked"


def test_the_async_video_path_agrees_with_the_sync_one(addon):
    from kosherd.vision import IMMODEST, ImageVerdict

    flow = _hflow("video/mp4", length=1000)
    filt = _filt(addon, media="immodest",
                 videos=_stub_videos(verdict=ImageVerdict(IMMODEST, (), True)))
    asyncio.run(filt.response(flow))
    assert flow.response.status_code == 403


def test_a_streamed_response_is_not_read_again_in_response(addon):
    flow = _hflow("video/mp4", length=1000)
    flow.response.stream = True

    class Never:
        def verdict(self, *a):
            raise AssertionError("there is no body to read")

    filt = _filt(addon, media="none", videos=Never(), vision=Never())
    asyncio.run(filt.response(flow))


# -- the picture path, off the event loop --------------------------------------

def _async_vision(verdict):
    async def verdict_async(data):
        return verdict

    async def run_async(fn, *args):
        return fn(*args)

    return type("V", (), {
        "verdict": staticmethod(lambda data: verdict),
        "verdict_async": staticmethod(verdict_async),
        "run_async": staticmethod(run_async),
        "write_status": staticmethod(lambda *a, **k: None),
    })()


def _real_photo():
    Image = pytest.importorskip("PIL.Image")
    import io

    image = Image.new("RGB", (300, 300), (10, 200, 10))
    for x in range(100, 200):
        for y in range(100, 200):
            image.putpixel((x, y), (255, 0, 0) if (x + y) % 2 else (0, 0, 255))
    out = io.BytesIO()
    image.save(out, "JPEG", quality=95)
    return out.getvalue()


def test_the_async_picture_path_covers_a_region(addon):
    from kosherd.vision import NSFW, ImageVerdict

    body = _real_photo()
    flow = _hflow("image/jpeg", body=body, length=len(body))
    filt = _filt(addon, media="nsfw",
                 vision=_async_vision(ImageVerdict(NSFW, ((120, 120, 40, 40),), True)))
    asyncio.run(filt.response(flow))
    assert flow.response.headers["x-kosheros"] == "image-covered=nsfw"
    assert flow.response.headers["content-type"] == "image/jpeg"
    assert "content-length" not in flow.response.headers
    assert flow.response.content != body
    # The covered picture is remembered, so the same picture again costs
    # no decode: the cache holds exactly one entry for it.
    assert len(filt.covers) == 1
    again = _hflow("image/jpeg", body=body, length=len(body))
    asyncio.run(filt.response(again))
    assert again.response.content == flow.response.content


def test_a_cover_that_would_take_most_of_the_picture_hides_it_whole(addon):
    from kosherd.vision import NSFW, ImageVerdict

    body = _real_photo()
    flow = _hflow("image/jpeg", body=body)
    filt = _filt(addon, media="nsfw",
                 vision=_async_vision(ImageVerdict(NSFW, ((20, 20, 260, 260),), True)))
    asyncio.run(filt.response(flow))
    assert flow.response.headers["x-kosheros"] == "image-hidden"  # a same-size tile now
    assert len(filt.covers) == 0, "nothing was frosted"


def test_hidden_pictures_drop_the_length_header(addon):
    flow = _hflow("image/jpeg", body=b"x" * 20_000, length=20_000)
    filt = _filt(addon, media="all", vision=_async_vision(None))
    asyncio.run(filt.response(flow))
    assert flow.response.content == addon.BLANK_PNG
    assert "content-length" not in flow.response.headers


# -- the cover style travels with the account ---------------------------------

def test_the_proxy_reads_the_cover_style_and_falls_closed_to_frost(addon, tmp_path):
    rules = tmp_path / "rules.json"
    rules.write_text(json.dumps({"1001": {"port": 30001, "rules": [], "cover_style": "skin"},
                                 "1002": {"port": 30002, "rules": []}}))
    cache = addon.PolicyCache(rules)
    assert cache.cover_style_for(1001) == "skin"
    assert cache.cover_style_for(1002) == "frost"
    assert cache.cover_style_for(4242) == "frost", "the unknown-uid floor"


def test_the_skin_style_does_not_hide_a_dominant_figure_whole(addon):
    from kosherd.vision import NSFW, ImageVerdict

    body = _real_photo()
    flow = _hflow("image/jpeg", body=body)
    filt = _filt(addon, media="nsfw",
                 vision=_async_vision(ImageVerdict(NSFW, ((20, 20, 260, 260),), True)))
    filt.policy = type("P", (), {
        "uid_for_listener_port": staticmethod(lambda port: None),
        "media_level_for": staticmethod(lambda uid: "nsfw"),
        "blocked_categories_for": staticmethod(lambda uid: []),
        "cover_style_for": staticmethod(lambda uid: "skin"),
    })()
    asyncio.run(filt.response(flow))
    assert flow.response.content != addon.BLANK_PNG, "painted, not hidden whole"
    assert flow.response.headers["x-kosheros"] == "image-covered=nsfw"


def test_an_animation_that_hides_is_hidden_whole_not_covered(addon):
    from kosherd.vision import NSFW, ImageVerdict
    from PIL import Image
    import io

    import random

    rng = random.Random(1)
    frames = []
    for colour in ((10, 200, 10), (220, 20, 20)):
        im = Image.new("RGB", (300, 300), colour)
        px = im.load()
        for _ in range(15_000):  # noise, so the GIF is a picture and not an icon
            px[rng.randrange(300), rng.randrange(300)] = (rng.randrange(256),) * 3
        frames.append(im)
    out = io.BytesIO()
    frames[0].save(out, "GIF", save_all=True, append_images=frames[1:], duration=100, loop=0)
    body = out.getvalue()
    assert len(body) > addon.MIN_IMAGE_BYTES
    flow = _hflow("image/gif", body=body)
    filt = _filt(addon, media="nsfw",
                 vision=_async_vision(ImageVerdict(NSFW, ((20, 20, 60, 60),), True)))
    asyncio.run(filt.response(flow))
    assert flow.response.headers["x-kosheros"] == "image-hidden"  # a same-size tile now
    assert len(filt.covers) == 0


# ---- what the filter did is written down ---------------------------------

def _activity_to(addon, tmp_path):
    import functools

    from kosherd import activity

    addon.activity_mod.record = functools.partial(activity.record, spool=tmp_path)
    return functools.partial(activity.events, spool=tmp_path)


def test_a_department_block_is_written_to_the_activity_log(addon, tmp_path):
    from pathlib import Path

    from kosherd import siterules

    events = _activity_to(addon, tmp_path)
    filt = addon.KosherFilter.__new__(addon.KosherFilter)
    filt.uids = type("U", (), {"uid_for_connection": staticmethod(lambda *a, **k: 1001)})()
    filt.policy = _request_policy(blocked=["immodest"])
    filt.categories = _stub_categories()
    filt.siterules = siterules.load(
        Path(__file__).parents[2] / "os-image/files/usr/share/kosher/site-rules.json")
    filt.blocklist = _stub_blocklist()

    flow = _request_flow("https://www.amazon.com/s?k=x&i=fashion-womens")
    filt.request(flow)
    found = events()
    assert len(found) == 1
    assert found[0]["kind"] == "block"
    assert found[0]["uid"] == 1001
    assert found[0]["url"].startswith("https://www.amazon.com/")
    assert found[0]["why"].startswith("shop:")


def test_the_block_page_carries_the_reason_into_a_request(addon, tmp_path):
    from kosherd import accessreq

    _activity_to(addon, tmp_path)
    filt = addon.KosherFilter.__new__(addon.KosherFilter)
    flow = _request_flow("https://example.com/x")
    filt._block(flow, "https://example.com/x", " because it is video",
                why="category:video")
    body = flow.response.content.decode()
    assert 'name="why" value="category:video"' in body

    filt.uids = type("U", (), {"uid_for_connection": staticmethod(lambda *a, **k: 1001)})()
    filt.policy = _request_policy()
    filt.categories = _stub_categories()
    filt.siterules = type("S", (), {
        "reason": staticmethod(lambda u: None),
        "blocked_search": staticmethod(lambda u: None),
        "search_text": staticmethod(lambda u: None),
    })()
    filt.blocklist = _stub_blocklist()
    ask = _request_flow("https://example.com" + addon.REQUEST_PATH)
    ask.request.method = "POST"
    ask.request.get_text = lambda strict=False: (
        "url=https%3A%2F%2Fexample.com%2Fx&note=&why=category%3Avideo")
    import functools
    addon.accessreq_mod.submit = functools.partial(accessreq.submit, spool=tmp_path / "req")
    (tmp_path / "req").mkdir()
    filt.request(ask)
    waiting = accessreq.pending(spool=tmp_path / "req")
    assert waiting[0]["why"] == "category:video"


def test_hidden_pictures_are_one_line_per_page_not_per_picture(addon, tmp_path):
    events = _activity_to(addon, tmp_path)
    filt = addon.KosherFilter.__new__(addon.KosherFilter)
    for _ in range(5):
        flow = _image_flow_sized(addon, 4000, referer="https://shop.example/dress")
        flow.metadata = {"kosher_uid": 1001}
        filt._blank_image(flow)
    found = events()
    assert len(found) == 1
    assert found[0]["kind"] == "pictures"
    assert found[0]["url"] == "https://shop.example/dress"

    other = _image_flow_sized(addon, 4000, referer="https://shop.example/shoes")
    other.metadata = {"kosher_uid": 1001}
    filt._blank_image(other)
    assert len(events()) == 2


def test_a_failed_note_never_fails_the_block(addon, tmp_path):
    addon.activity_mod.SPOOL_DIR = tmp_path / "nowhere"
    filt = addon.KosherFilter.__new__(addon.KosherFilter)
    flow = _request_flow("https://example.com/x")
    filt._block(flow, "https://example.com/x", "", why="rule:example.com")
    assert flow.response.status_code == 403
