"""Calmer versions of the addictive sites (#179).

x.com, reddit, YouTube and facebook are bottomless, and cannot always be
blocked wholesale — an article links a tweet, a reddit post answers a bug.
The account gets a calmer version instead: a feed ends after a set number
of items, a linked post opens on its own, the ads, sidebars and
recommendations are never shown, and a site may carry a daily budget that
counts only while one of its pages is on screen.

The pages are single-page apps, so the rewriting is done in the page by
the script the proxy injects (mitm/kosher_calm.js). These tests cover the
part the two sides agree on (calm.py: hosts, post-or-feed, the shape of a
policy), the proxy side (injection, the budget, the heartbeat), and the
script itself, driven by a stand-in browser (calm_harness.js).
"""

import json
import re
import shutil
import types
from pathlib import Path

import pytest

from kosherd import calm as calm_mod
from kosherd import policy as policy_mod
from kosherd import usage as usage_mod
from test_mitm_addon import addon  # noqa: F401  (the addon with mitmproxy stubbed)

ROOT = Path(__file__).parents[2]
SCRIPT = ROOT / "mitm/kosher_calm.js"
HARNESS = Path(__file__).with_name("calm_harness.js")
SCHEMAS = (ROOT / "policy/schema/policy.schema.json",
           ROOT / "kosherd/src/kosherd/data/policy.schema.json")


# -- the sites, and which page is which ---------------------------------------

def test_each_host_belongs_to_its_site():
    assert calm_mod.site_for("x.com") == "x"
    assert calm_mod.site_for("www.x.com") == "x"
    assert calm_mod.site_for("mobile.twitter.com") == "x"
    assert calm_mod.site_for("old.reddit.com") == "reddit"
    assert calm_mod.site_for("youtube-nocookie.com") == "youtube"
    assert calm_mod.site_for("m.facebook.com") == "facebook"
    # Lookalikes are somebody else's site.
    assert calm_mod.site_for("notx.com") is None
    assert calm_mod.site_for("x.com.example.test") is None
    assert calm_mod.site_for("") is None


def test_a_login_is_never_touched_a_post_is_alone_and_the_rest_is_a_feed():
    assert calm_mod.mode_for("x", "/i/flow/login") == "login"
    assert calm_mod.mode_for("x", "/user/status/12345") == "single"
    assert calm_mod.mode_for("x", "/home") == "feed"
    # Signing into YouTube happens on accounts.google.com, off the site.
    assert calm_mod.mode_for("youtube", "/watch?v=abc") == "single"
    assert calm_mod.mode_for("youtube", "/feed/subscriptions") == "feed"
    assert calm_mod.mode_for("reddit", "/r/linux/comments/abc/a_bug/") == "single"
    assert calm_mod.mode_for("reddit", "/r/linux/") == "feed"
    assert calm_mod.mode_for("facebook", "/someone/posts/123") == "single"
    assert calm_mod.mode_for("facebook", "/login") == "login"


def test_the_paths_rules_are_the_same_answer_the_script_gets():
    """The script re-reads the address on every soft navigation, so the
    proxy hands it the same rules, not a second copy that can drift."""
    paths = ["/i/flow/login", "/home", "/user/status/1", "/watch?v=1",
             "/r/linux/comments/a/b", "/r/linux", "/login", "/someone/posts/9"]
    for site in calm_mod.SITE_ORDER:
        rules = calm_mod.path_rules(site)
        for path in paths:
            single = any(re.search(source, path) for source in rules["single"])
            assert single == calm_mod.is_single(site, path), (site, path)
            login = any(path.lower().startswith(prefix)
                        for prefix in rules["login"])
            assert login == calm_mod.is_login(site, path), (site, path)


# -- what a policy's `calm` may look like -------------------------------------

def test_nothing_listed_means_off():
    assert calm_mod.normalize(None) == {}
    assert calm_mod.normalize({}) == {}
    # On for no site is off: the site checklist is what the feature is.
    assert calm_mod.normalize({"enabled": True, "sites": {}}) == {}
    assert calm_mod.normalize({"enabled": False, "sites": {"x": {}}}) == {}


def test_a_canonical_setting_is_kept_and_defaults_dropped():
    settings = calm_mod.normalize({"enabled": True, "feed_items": 20,
                                   "sites": {"x": {"daily_minutes": 15},
                                             "reddit": True}})
    assert settings == {"enabled": True, "feed_items": 20,
                        "sites": {"x": {"daily_minutes": 15}, "reddit": {}}}
    assert calm_mod.enabled_sites(settings) == ["x", "reddit"]
    assert calm_mod.applies(settings, "x") and not calm_mod.applies(settings, "youtube")
    assert calm_mod.feed_items(settings) == 20
    assert calm_mod.daily_minutes(settings, "x") == 15
    assert calm_mod.daily_minutes(settings, "reddit") == 0
    # Display order, not the order the admin app happened to send.
    assert calm_mod.enabled_sites({"enabled": True,
                                   "sites": {"facebook": {}, "x": {}}}) == ["x", "facebook"]


@pytest.mark.parametrize("settings", [
    {"feed_items": 1},
    {"feed_items": 501},
    {"feed_items": "30"},
    {"feed_items": True},
    {"sites": {"myspace": {}}},
    {"sites": {"x": {"daily_minutes": 1441}}},
    {"sites": {"x": {"daily_minutes": -1}}},
    {"sites": {"x": {"wasted_hours": 2}}},
    {"nonsense": 1},
    [],
])
def test_what_the_admin_app_could_not_have_sent_is_refused(settings):
    with pytest.raises(ValueError):
        calm_mod.normalize(settings)


# -- the schema both sides read ------------------------------------------------

def test_both_schema_copies_carry_the_calm_settings():
    texts = [path.read_text() for path in SCHEMAS]
    assert texts[0] == texts[1], "the two schema copies disagree"
    schema = json.loads(texts[0])
    calm = schema["$defs"]["calm"]
    assert set(calm["properties"]) == {"enabled", "feed_items", "sites"}
    assert set(calm["properties"]["sites"]["properties"]) == set(calm_mod.SITE_ORDER)
    assert schema["$defs"]["user"]["properties"]["calm"]["$ref"] == "#/$defs/calm"
    assert schema["properties"]["guest"]["properties"]["calm"]["$ref"] == "#/$defs/calm"


def test_a_policy_keeps_the_calm_settings_through_a_round_trip():
    doc = json.loads((ROOT / "policy/examples/family.json").read_text())
    calm = {"enabled": True, "feed_items": 25,
            "sites": {"x": {"daily_minutes": 20}, "youtube": {}}}
    doc["users"][0]["calm"] = calm
    policy = policy_mod.Policy.from_dict(doc)
    assert policy.user(doc["users"][0]["uid"]).calm == calm
    assert policy.to_dict()["users"][0]["calm"] == calm


def test_the_guest_can_have_calmer_sites_too():
    doc = json.loads((ROOT / "policy/examples/family.json").read_text())
    calm = {"enabled": True, "sites": {"reddit": {"daily_minutes": 10}}}
    doc["guest"]["calm"] = calm
    policy = policy_mod.Policy.from_dict(doc)
    assert policy.guest.calm == calm
    assert policy.to_dict()["guest"]["calm"] == calm


# -- the proxy: serving the script, injecting it, keeping the budget ----------

def _policy(**calm):
    return types.SimpleNamespace(calm_for=lambda uid: dict(calm))


class _Response:
    def __init__(self, text, headers, status=200, url=""):
        self.text = text
        self.headers = headers
        self.status_code = status
        self._url = url

    def get_text(self, strict=False):
        return self.text

    @property
    def content(self):
        return self.text.encode()


def _flow(addon, url="https://x.com/home", method="GET", body=b"", headers=None,
          html=None, status=200):
    from urllib.parse import urlsplit

    parts = urlsplit(url)
    path = parts.path + (f"?{parts.query}" if parts.query else "")
    request = types.SimpleNamespace(
        method=method, path=path, pretty_host=parts.hostname, pretty_url=url,
        headers=dict(headers or {}),
        get_text=lambda strict=False: body.decode())
    response = None
    if html is not None:
        response = _Response(html, {"content-type": "text/html; charset=utf-8"},
                             status=status)
    return types.SimpleNamespace(request=request, response=response,
                                 metadata={"kosher_uid": 1001})


def _filter(addon, tmp_path, **calm):
    filt = addon.KosherFilter.__new__(addon.KosherFilter)
    filt.policy = _policy(**calm)
    filt._calm_time_path = str(tmp_path / "calm-time.json")
    filt.notes = []
    filt._note = lambda kind, flow, **fields: filt.notes.append((kind, fields))
    return filt


HTML = "<html><head></head><body><p>a page</p></body></html>"
X_ON = {"enabled": True, "feed_items": 20, "sites": {"x": {"daily_minutes": 0}}}
X_BUDGET = {"enabled": True, "sites": {"x": {"daily_minutes": 10}}}


def test_the_script_is_served_on_every_host(addon):
    filt = _filter(addon, Path("/tmp"))
    flow = _flow(addon, "https://anything.example/__kosheros__/calm.js")
    assert filt._serve_calm(flow, 1001) is True
    assert flow.response.status_code == 200
    assert flow.response.content == SCRIPT.read_bytes()
    assert "javascript" in flow.response.headers["Content-Type"]
    etag = {"if-none-match": flow.response.headers["ETag"]}
    again = _flow(addon, "https://x.com/__kosheros__/calm.js", headers=etag)
    assert filt._serve_calm(again, 1001) is True
    assert again.response.status_code == 304  # asked twice, same script


def test_a_page_of_a_calmed_site_gets_the_script(addon, tmp_path):
    filt = _filter(addon, tmp_path, **X_ON)
    flow = _flow(addon, html=HTML)
    filt._calm_filter(flow, 1001)
    body = flow.response.text
    assert "__kosheros__/calm.js" in body
    assert 'data-kosheros-calm-site="x"' in body
    assert 'data-kosheros-calm-items="20"' in body
    assert 'data-kosheros-calm-remaining="-1"' in body  # no daily budget
    # The rules that let the script re-read the address itself, escaped.
    assert "data-kosheros-calm-rules=" in body
    assert "&quot;single&quot;" in body
    assert flow.response.headers["x-kosheros-calm"] == "x:feed"
    # The page's own markup is still there.
    assert "<p>a page</p>" in body


def test_a_post_gets_the_single_mode_and_a_login_is_left_alone(addon, tmp_path):
    filt = _filter(addon, tmp_path, **X_ON)
    post = _flow(addon, "https://x.com/someone/status/123", html=HTML)
    filt._calm_filter(post, 1001)
    assert 'data-kosheros-calm-mode="single"' in post.response.text
    login = _flow(addon, "https://x.com/i/flow/login", html=HTML)
    filt._calm_filter(login, 1001)
    assert login.response.text == HTML
    assert "calm.js" not in login.response.text


def test_a_site_that_is_not_calmed_and_a_page_that_was_blocked_are_untouched(addon, tmp_path):
    filt = _filter(addon, tmp_path, **X_ON)
    other = _flow(addon, "https://example.test/page", html=HTML)
    filt._calm_filter(other, 1001)
    assert other.response.text == HTML
    blocked = _flow(addon, html=HTML, status=403)
    filt._calm_filter(blocked, 1001)
    assert blocked.response.text == HTML


def test_a_page_that_is_not_html_is_left_alone(addon, tmp_path):
    filt = _filter(addon, tmp_path, **X_ON)
    flow = _flow(addon, "https://x.com/home", html="<html></html>")
    flow.response.headers["content-type"] = "application/json"
    filt._calm_filter(flow, 1001)
    assert flow.response.text == "<html></html>"


def test_a_spent_budget_closes_the_site_for_the_day(addon, tmp_path):
    filt = _filter(addon, tmp_path, **X_BUDGET)
    filt._calm_time = {f"1001:x": {"day": usage_mod.day_key(), "seconds": 600}}
    flow = _flow(addon, html=HTML)
    filt._calm_filter(flow, 1001)
    assert flow.response.status_code == 403
    assert "Enough X (Twitter) for today" in flow.response.content.decode()
    assert filt.notes and filt.notes[0][1]["why"] == "calm:x"
    # The same site's other page, and the next page load, are closed too.
    again = _flow(addon, "https://x.com/someone/status/1", html=HTML)
    filt._calm_filter(again, 1001)
    assert again.response.status_code == 403


def test_a_spent_budget_does_not_touch_another_calmed_site(addon, tmp_path):
    settings = {"enabled": True, "sites": {"x": {"daily_minutes": 10},
                                          "reddit": {"daily_minutes": 10}}}
    filt = _filter(addon, tmp_path, **settings)
    filt._calm_time = {f"1001:x": {"day": usage_mod.day_key(), "seconds": 600}}
    reddit = _flow(addon, "https://www.reddit.com/r/linux/", html=HTML)
    filt._calm_filter(reddit, 1001)
    assert reddit.response.status_code == 200
    assert "calm.js" in reddit.response.text


def test_the_budget_is_written_out_so_a_restart_does_not_hand_time_back(addon, tmp_path):
    filt = _filter(addon, tmp_path, **X_BUDGET)
    filt._calm_credit(1001, "x", 90, 10)
    filt._calm_flush(force=True)
    saved = json.loads((tmp_path / "calm-time.json").read_text())
    assert saved[f"1001:x"]["seconds"] == 90
    assert saved[f"1001:x"]["day"] == usage_mod.day_key()
    # A fresh proxy reads it back, and yesterday's buckets are dropped.
    yesterday = {"1001:reddit": {"day": "1999-01-01", "seconds": 500}}
    (tmp_path / "calm-time.json").write_text(json.dumps({**saved, **yesterday}))
    fresh = _filter(addon, tmp_path, **X_BUDGET)
    assert fresh._calm_remaining(1001, "x", 10) == 510
    assert f"1001:reddit" not in fresh._calm_store()


def test_a_heartbeat_counts_the_seconds_and_says_what_is_left(addon, tmp_path):
    settings = {"enabled": True, "sites": {"x": {"daily_minutes": 3}}}
    filt = _filter(addon, tmp_path, **settings)

    def beat(seconds):
        flow = _flow(addon, "https://x.com/__kosheros__/calm-beat", method="POST",
                     body=json.dumps({"url": "https://x.com/home",
                                      "seconds": seconds}).encode(),
                     headers={"X-KosherOS-Page": filt._page_token(1001, "x.com")})
        assert filt._serve_calm(flow, 1001) is True
        return json.loads(flow.response.content)

    assert beat(60) == {"remaining": 120, "over": False}
    assert beat(60) == {"remaining": 60, "over": False}
    # Spent: the answer carries the page that replaces this one.
    spent = beat(60)
    assert spent["over"] is True
    assert "Enough X (Twitter) for today" in spent["page"]


def test_a_heartbeat_may_not_credit_more_than_its_cap(addon, tmp_path):
    filt = _filter(addon, tmp_path, **X_BUDGET)
    flow = _flow(addon, "https://x.com/__kosheros__/calm-beat", method="POST",
                 body=json.dumps({"url": "https://x.com/home",
                                  "seconds": 100000}).encode(),
                 headers={"X-KosherOS-Page": filt._page_token(1001, "x.com")})
    filt._serve_calm(flow, 1001)
    # A page may claim what it likes; the proxy credits one cap, not a day.
    assert filt._calm_store()["1001:x"]["seconds"] == addon.CALM_BEAT_MAX_SECONDS
    assert json.loads(flow.response.content)["over"] is False


def test_a_heartbeat_without_the_pages_token_or_from_another_page_is_refused(addon, tmp_path):
    filt = _filter(addon, tmp_path, **X_BUDGET)
    beat = {"url": "https://x.com/home", "seconds": 10}
    no_token = _flow(addon, "https://x.com/__kosheros__/calm-beat", method="POST", body=json.dumps(beat).encode())
    assert filt._serve_calm(no_token, 1001) is True
    assert no_token.response.status_code == 403
    # A page on x.com may not report time for reddit.com.
    other = _flow(addon, "https://x.com/__kosheros__/calm-beat", method="POST", body=json.dumps(
        {"url": "https://reddit.com/r/linux/", "seconds": 10}).encode(),
        headers={"X-KosherOS-Page": filt._page_token(1001, "x.com")})
    filt._serve_calm(other, 1001)
    assert other.response.status_code == 403


def test_a_heartbeat_for_a_site_that_is_not_calmed_gets_nothing(addon, tmp_path):
    # The account calms x, not youtube: a youtube page's word means nothing.
    filt = _filter(addon, tmp_path, **X_ON)
    flow = _flow(addon, "https://www.youtube.com/__kosheros__/calm-beat", method="POST",
                 body=json.dumps({"url": "https://www.youtube.com/watch?v=1",
                                  "seconds": 10}).encode(),
                 headers={"X-KosherOS-Page": filt._page_token(1001, "www.youtube.com")})
    filt._serve_calm(flow, 1001)
    assert flow.response.status_code == 404


def test_the_budget_answer_is_never_cached(addon, tmp_path):
    filt = _filter(addon, tmp_path, **X_BUDGET)
    flow = _flow(addon, "https://x.com/__kosheros__/calm-beat", method="POST",
                 body=json.dumps({"url": "https://x.com/home", "seconds": 1}).encode(),
                 headers={"X-KosherOS-Page": filt._page_token(1001, "x.com")})
    filt._serve_calm(flow, 1001)
    assert flow.response.headers["Cache-Control"] == "no-store"


# -- the script itself ---------------------------------------------------------

node = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


@node
def test_the_script_parses():
    import subprocess

    subprocess.run(["node", "--check", str(SCRIPT)], check=True)


def _harness(site, mode, items, path, budget="-1"):
    import subprocess

    out = subprocess.run(["node", str(HARNESS), str(SCRIPT), site, mode,
                          str(items), path, str(budget)],
                         capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


@node
def test_a_feed_ends_after_the_set_number_of_items():
    state = _harness("x", "feed", 4, "/home")
    assert state["hidden"] == [False, False, False, False, True, True]
    assert state["endCard"] is True
    # The sidebar never shows either.
    assert state["sidebarHidden"] is True


@node
def test_each_site_is_capped_by_its_own_names():
    youtube = _harness("youtube", "feed", 2, "/feed/subscriptions")
    assert youtube["hidden"] == [False, False, True, True, True, True]
    assert youtube["sidebarHidden"] and youtube["relatedHidden"] and youtube["commentsHidden"]
    facebook = _harness("facebook", "feed", 3, "/")
    assert facebook["hidden"] == [False, False, False, True, True, True]
    assert facebook["asideHidden"] is True


@node
def test_a_post_on_its_own_hides_the_thread_and_the_rails():
    state = _harness("x", "single", 30, "/someone/status/12")
    assert state["hidden"] == [False, True, True, True, True, True]
    assert state["endCard"] is False
    youtube = _harness("youtube", "single", 30, "/watch")
    assert youtube["commentsHidden"] and youtube["sidebarHidden"]


@node
def test_a_login_page_is_never_rewritten():
    state = _harness("x", "feed", 4, "/i/flow/login")
    assert state["hidden"] == [False] * 6
    assert state["sidebarHidden"] is False
    assert state["endCard"] is False


@node
def test_a_budgeted_page_sends_a_heartbeat_and_none_is_sent_without_one():
    budgeted = _harness("x", "feed", 4, "/home", 60)
    assert budgeted["beats"] == [{"url": "https://x.com/home", "seconds": 15}]
    unbudgeted = _harness("x", "feed", 4, "/home", -1)
    assert unbudgeted["beats"] == []


@node
@pytest.mark.parametrize("site, path", [
    ("x", "/someone/status/12"),
    ("reddit", "/r/linux/comments/1/title/"),
    ("youtube", "/watch"),
    ("facebook", "/someone/posts/12"),
])
def test_in_a_post_same_site_links_go_nowhere_and_links_out_work(site, path):
    links = _harness(site, "single", 30, path)["links"]
    # [href, stopped, marked]: the first two are the same site, the third is
    # the site's own way out, the last goes straight out.
    assert [stopped for _href, stopped, _marked in links] == [True, True, False, False]
    assert [marked for _href, _stopped, marked in links] == [True, True, False, False]


@node
def test_in_a_feed_links_are_left_alone():
    links = _harness("x", "feed", 30, "/home")["links"]
    assert not any(stopped or marked for _href, stopped, marked in links)
