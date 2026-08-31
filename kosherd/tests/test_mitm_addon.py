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
    http_mod.Response = types.SimpleNamespace(make=lambda *a, **k: ("response", a))
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


def test_categories_are_empty_for_unknown_users(addon, tmp_path):
    cache = addon.PolicyCache(_rules_file(tmp_path, {}))
    assert cache.blocked_categories_for(4242) == []
    assert cache.blocked_categories_for(None) == []


# -- media level and YouTube --------------------------------------------------

def test_cache_reads_media_level_and_youtube(addon, tmp_path):
    path = _rules_file(tmp_path, {"1001": {
        "rules": [], "blocked_categories": [], "media_level": "immodest",
        "youtube": {"allowed_channels": ["@torah"], "restrict": "strict"}}})
    cache = addon.PolicyCache(path)
    assert cache.media_level_for(1001) == "immodest"
    assert cache.youtube_for(1001)["restrict"] == "strict"
    assert cache.media_level_for(4242) == "none"  # unknown user unaffected


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
