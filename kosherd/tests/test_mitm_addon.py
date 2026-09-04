"""Tests for the mitmproxy addon's uid lookup and policy cache.

The addon itself imports mitmproxy, which the dev shell doesn't have, so we
test the two pieces that carry the real risk — parsing /proc/net/tcp and
reloading the policy — by loading the module with a stub in place.
"""

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
    assert lookup.uid_for_port(8080) == 989
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
        "media_level_for": staticmethod(lambda uid: media),
        "blocked_categories_for": staticmethod(lambda uid: list(blocked)),
    })()


def _stub_blocklist():
    return type("B", (), {"contains_any": staticmethod(lambda text: False)})()


def _stub_categories():
    return type("C", (), {
        "blocked_categories_of": staticmethod(lambda host, blocked: set()),
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
    filt.uids = type("U", (), {"uid_for_port": staticmethod(lambda p: 1001)})()
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
    filt.uids = type("U", (), {"uid_for_port": staticmethod(lambda p: 1001)})()
    filt.policy = _request_policy(blocked=["gambling"])
    filt.categories = _stub_categories()
    filt.siterules = rules
    filt.blocklist = _stub_blocklist()

    flow = _request_flow("https://www.amazon.com/s?k=x&i=fashion-womens")
    filt.request(flow)
    assert flow.response is None


def _request_policy(blocked=()):
    return type("P", (), {
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
    filt.uids = type("U", (), {"uid_for_port": staticmethod(lambda p: 1001)})()
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
    filt.uids = type("U", (), {"uid_for_port": staticmethod(lambda p: 1001)})()
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
    filt.uids = type("U", (), {"uid_for_port": staticmethod(lambda p: 1001)})()
    filt.policy = type("P", (), {
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
    filt.response(flow)
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
    filt.response(flow)
    assert "content-length" not in flow.response.headers


def test_suggestions_are_left_alone_for_accounts_that_did_not_ask(addon):
    import json

    filt = _shop_filter(addon, blocked=("gambling",))
    body = json.dumps(["lamp", "lingerie"]).encode()
    flow = _suggestion_flow(
        "https://completion.amazon.com/api/2017/suggestions?prefix=l", body)
    filt.response(flow)
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
