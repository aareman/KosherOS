"""Finding a YouTube channel by name, for the approved-channel list.

Approving a channel used to mean digging its ID out of a video's address.
The daemon now asks YouTube's own search for channels and hands back the
ID the filter matches on beside the name a parent recognises.
"""

import json
import urllib.error

import pytest

from kosherd import access, ytsearch
from kosherd.access import ACTIONS, CHANGES, GUARDIAN_GATED
from kosherd.daemon import IN_BACKGROUND, Daemon, PolicyError
from kosherd.policy import Policy


def _renderer(channel_id, title, handle=None, subscribers=None):
    """A channelRenderer shaped like YouTube's: since handles arrived the
    handle sits in subscriberCountText and the count in videoCountText."""
    renderer = {"channelId": channel_id, "title": {"simpleText": title}}
    if handle:
        renderer["navigationEndpoint"] = {"browseEndpoint": {
            "browseId": channel_id, "canonicalBaseUrl": f"/{handle}"}}
        renderer["subscriberCountText"] = {"simpleText": handle}
    if subscribers:
        renderer["videoCountText"] = {"simpleText": subscribers}
    return {"channelRenderer": renderer}


def _answer(*items):
    return {"contents": {"twoColumnSearchResultsRenderer": {"primaryContents": {
        "sectionListRenderer": {"contents": [
            {"itemSectionRenderer": {"contents": list(items)}}]}}}}}


TORAH = "UC7BFmSXP4mHMNSvWUaqg2uQ"
OTHER = "UCaaaaaaaaaaaaaaaaaaaaaa"


# ---- reading the answer -----------------------------------------------------

def test_a_channel_comes_back_with_its_id_handle_and_subscribers():
    found = ytsearch.parse_channels(_answer(
        _renderer(TORAH, "TorahAnytime", "@torahanytime1", "12.8K subscribers")))
    assert found == [{"id": TORAH, "title": "TorahAnytime",
                      "handle": "@torahanytime1", "subscribers": "12.8K subscribers"}]


def test_the_handle_is_never_mistaken_for_the_subscriber_count():
    # The field YouTube calls subscriberCountText holds the handle.
    found = ytsearch.parse_channels(_answer(_renderer(TORAH, "TorahAnytime", "@torah")))
    assert "subscribers" not in found[0]
    assert found[0]["handle"] == "@torah"


def test_youtube_s_order_is_kept_and_each_channel_listed_once():
    found = ytsearch.parse_channels(_answer(
        _renderer(OTHER, "Second"), _renderer(TORAH, "First"), _renderer(OTHER, "Second")))
    assert [c["id"] for c in found] == [OTHER, TORAH]


def test_titles_split_into_runs_are_joined():
    item = _renderer(TORAH, "")
    item["channelRenderer"]["title"] = {"runs": [{"text": "Torah"}, {"text": "Anytime"}]}
    assert ytsearch.parse_channels(_answer(item))[0]["title"] == "TorahAnytime"


def test_anything_that_is_not_a_channel_is_skipped_not_an_error():
    found = ytsearch.parse_channels(_answer(
        {"videoRenderer": {"videoId": "x", "channelId": OTHER}},
        {"channelRenderer": {"title": {"simpleText": "no id"}}},
        {"channelRenderer": {"channelId": "not-a-channel-id"}},
        {"channelRenderer": "not even a dict"},
        _renderer(TORAH, "TorahAnytime")))
    assert [c["id"] for c in found] == [TORAH]


@pytest.mark.parametrize("doc", [None, [], "", {"error": {"code": 400}}])
def test_an_answer_with_no_channels_is_an_empty_list(doc):
    assert ytsearch.parse_channels(doc) == []


# ---- asking YouTube ---------------------------------------------------------

class _Response:
    def __init__(self, body):
        self.body = body

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_the_search_asks_for_channels_only(monkeypatch):
    sent = {}

    def urlopen(request, timeout):
        sent.update(json.loads(request.data))
        return _Response(json.dumps(_answer(_renderer(TORAH, "TorahAnytime"))).encode())

    monkeypatch.setattr(ytsearch.urllib.request, "urlopen", urlopen)
    assert ytsearch.search_channels("  torah anytime ")[0]["id"] == TORAH
    assert sent["query"] == "torah anytime"
    assert sent["params"] == ytsearch.CHANNELS_ONLY


def test_an_empty_query_never_reaches_youtube(monkeypatch):
    def urlopen(*_a, **_k):
        raise AssertionError("asked YouTube for nothing")

    monkeypatch.setattr(ytsearch.urllib.request, "urlopen", urlopen)
    assert ytsearch.search_channels("   ") == []


@pytest.mark.parametrize("failure,words", [
    (urllib.error.URLError("Name or service not known"), "cannot reach YouTube"),
    (urllib.error.HTTPError("u", 429, "Too Many", {}, None), "HTTP 429"),
    (TimeoutError("timed out"), "cannot reach YouTube"),
])
def test_a_failed_search_says_why_in_words(monkeypatch, failure, words):
    def urlopen(*_a, **_k):
        raise failure

    monkeypatch.setattr(ytsearch.urllib.request, "urlopen", urlopen)
    with pytest.raises(ytsearch.SearchError, match=words):
        ytsearch.search_channels("torah")


def test_an_unreadable_answer_is_a_search_error(monkeypatch):
    monkeypatch.setattr(ytsearch.urllib.request, "urlopen",
                        lambda *_a, **_k: _Response(b"<html>consent</html>"))
    with pytest.raises(ytsearch.SearchError, match="unreadable"):
        ytsearch.search_channels("torah")


# ---- the daemon -------------------------------------------------------------

def test_searching_is_a_read_that_needs_no_guardian():
    # Approving the channel is SetYouTube, which is gated; finding one
    # changes nothing and is not an entry in the activity log.
    assert ACTIONS["SearchYouTubeChannels"] == access.ACTION_READ_CONFIG
    assert "SearchYouTubeChannels" not in GUARDIAN_GATED
    assert "SearchYouTubeChannels" not in CHANGES


def test_the_search_is_answered_off_the_main_loop():
    assert "SearchYouTubeChannels" in IN_BACKGROUND


def _daemon():
    daemon = Daemon.__new__(Daemon)
    daemon.policy = Policy(revision=1)
    return daemon


def test_the_daemon_hands_back_the_channels_as_json(monkeypatch):
    monkeypatch.setattr(ytsearch, "search_channels",
                        lambda q: [{"id": TORAH, "title": q}])
    answer = _daemon().impl_SearchYouTubeChannels("TorahAnytime").unpack()[0]
    assert json.loads(answer) == [{"id": TORAH, "title": "TorahAnytime"}]


def test_a_failed_search_reaches_the_admin_as_its_own_words(monkeypatch):
    # A PolicyError is passed through as it is; anything else would read
    # "internal error" in the admin app.
    def fail(_q):
        raise ytsearch.SearchError("cannot reach YouTube: offline")

    monkeypatch.setattr(ytsearch, "search_channels", fail)
    with pytest.raises(PolicyError, match="cannot reach YouTube: offline"):
        _daemon().impl_SearchYouTubeChannels("torah")
