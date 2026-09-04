"""The search page itself.

These run the real HTTP server against a stubbed metasearch backend, so
the things being tested are the ones a person actually meets: what the
page says when a search is refused, what a whitelist user sees before
they have searched anything, and whether a hidden result can leak into
the HTML anyway.
"""

import json
import threading
import urllib.request
from pathlib import Path

import pytest

from kosher_search import server as server_mod
from kosherd import content, search as search_mod

ROOT = Path(__file__).parents[2]
TERMS = ROOT / "os-image/files/usr/share/kosher/content-terms.json"
BLOCKLIST = ROOT / "os-image/files/usr/share/kosher/search-blocklist.json"

UID = 1001


class FakeBundle:
    def categories_of(self, host):
        return {"adult"} if "pornhub" in host else set()

    def blocked_categories_of(self, host, blocked):
        return self.categories_of(host) & set(blocked)


class FakeBackend:
    def __init__(self, results=None, fail=False):
        self.results = results or []
        self.fail = fail
        self.queries = []

    def search(self, query, category, page):
        if self.fail:
            raise OSError("backend down")
        self.queries.append((query, category, page))
        return {"results": self.results}


def start(user, results=None, fail=False):
    """Run the real server on an ephemeral port with a stubbed backend."""
    policy = search_mod.SearchPolicy(Path("/nonexistent"))
    policy._users = {UID: user}
    policy._refresh = lambda: None

    server_mod.Handler.backend = FakeBackend(results, fail)
    server_mod.Handler.result_filter = search_mod.ResultFilter(
        policy=policy, bundle=FakeBundle(), scorer=content.load(TERMS),
        blocklist=search_mod.load_blocklist(BLOCKLIST),
        scanner=_no_scanner())
    server_mod.Handler.uids = type("Stub", (), {
        "uid_for_connection": staticmethod(lambda *a, **k: UID)})()
    httpd = server_mod.Server(("127.0.0.1", 0), server_mod.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd


def _no_scanner():
    from kosherd import pagescan

    class Never(pagescan.PageScanner):
        def __init__(self):
            pass

        def verdicts(self, pairs):
            return {}

    return Never()


@pytest.fixture
def get():
    servers = []

    def start_and_get(user, path, results=None, fail=False):
        httpd = start(user, results, fail)
        servers.append(httpd)
        port = httpd.server_address[1]
        try:
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}{path}", timeout=5) as response:
                return response.status, response.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()

    yield start_and_get
    for httpd in servers:
        httpd.shutdown()
        httpd.server_close()


def test_a_search_shows_the_results_that_survive(get):
    results = [
        {"url": "https://chinuch.org/a", "title": "Lessons", "content": "Torah"},
        {"url": "https://pornhub.com/x", "title": "bad", "content": "bad"},
    ]
    status, body = get({"mode": "dnsfilter", "blocked_categories": ["adult"]},
                       "/search?q=torah", results)
    assert status == 200
    assert "chinuch.org/a" in body
    # Not merely hidden from view: not in the HTML at all.
    assert "pornhub" not in body
    assert "1 result(s) hidden" in body


def test_a_blocked_query_never_reaches_the_engine(get):
    httpd = start({"mode": "filtered"})
    port = httpd.server_address[1]
    try:
        with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/search?q=free+porn+videos") as r:
            body = r.read().decode()
    finally:
        httpd.shutdown()
        httpd.server_close()
    assert "This search is blocked" in body
    assert server_mod.Handler.backend.queries == []


def test_a_whitelist_user_is_shown_what_they_can_open(get):
    # Until now this was information the computer had and would not share.
    status, body = get(
        {"mode": "whitelist", "whitelist": ["chinuch.org", "torahanytime.com"]},
        "/")
    assert status == 200
    assert "Sites you can visit" in body
    assert "chinuch.org" in body and "torahanytime.com" in body


def test_a_whitelist_user_with_nothing_allowed_is_told_who_to_ask(get):
    status, body = get({"mode": "whitelist", "whitelist": []}, "/")
    assert "No sites are allowed yet" in body
    assert "administrator" in body


def test_an_unknown_user_is_shown_nothing(get):
    server_mod.Handler.uids = None  # replaced by start(); guard against reuse
    results = [{"url": "https://chinuch.org/a", "title": "Lessons", "content": ""}]
    status, body = get({"mode": "none"}, "/search?q=torah", results)
    assert "chinuch.org/a" not in body


def test_a_backend_outage_says_so_instead_of_showing_nothing(get):
    # "No results" would be a lie, and a lie that reads as censorship.
    status, body = get({"mode": "filtered"}, "/search?q=torah", fail=True)
    assert status == 503
    assert "Search is unavailable" in body


def test_results_are_never_cached(get):
    httpd = start({"mode": "filtered"})
    port = httpd.server_address[1]
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/search?q=torah") as r:
            assert r.headers["Cache-Control"] == "no-store"
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_a_hostile_result_cannot_inject_markup(get):
    results = [{"url": "https://chinuch.org/a",
                "title": "<script>alert(1)</script>",
                "content": "<img src=x onerror=alert(2)>"}]
    status, body = get({"mode": "filtered"}, "/search?q=torah", results)
    # The text may appear; a live tag may not.
    assert "<script>alert(1)</script>" not in body
    assert "<img src=x" not in body
    assert "&lt;script&gt;" in body and "&lt;img src=x" in body


def test_the_query_is_escaped_back_into_the_search_box(get):
    status, body = get({"mode": "filtered"}, '/search?q=%22%3E%3Cscript%3Ex')
    assert "<script>x" not in body


def test_opensearch_lets_the_browser_add_the_engine(get):
    status, body = get({"mode": "filtered"}, "/opensearch.xml")
    assert status == 200
    assert "OpenSearchDescription" in body
    assert "{searchTerms}" in body


def test_safe_search_is_forced_on_every_backend_call(monkeypatch):
    # The one request the engines actually see. A user who reaches the
    # front end must not be able to turn this off.
    captured = {}

    class Fake:
        headers = {}

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b"{}"

    def fake_urlopen(request, timeout=None):
        captured["url"] = request.full_url
        return Fake()

    # monkeypatch, so the real urlopen is restored even if this fails —
    # an earlier version patched it by hand and leaked into every test
    # that came after.
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    server_mod.Backend("http://127.0.0.1:1").search("torah", "general", 1)
    assert "safesearch=2" in captured["url"]


# -- asking from the search page ----------------------------------------------

def post(user, path, body, results=None, spool=None):
    from kosherd import accessreq

    httpd = start(user, results)
    if spool is not None:
        accessreq.SPOOL_DIR = spool
    port = httpd.server_address[1]
    try:
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}{path}", data=body.encode(),
            headers={"Content-Type": "application/x-www-form-urlencoded"})
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, response.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_a_whitelist_user_can_ask_for_a_site(tmp_path):
    from kosherd import accessreq

    status, body = post({"mode": "whitelist", "whitelist": []},
                        server_mod.REQUEST_PATH,
                        "url=https%3A%2F%2Fchinuch.org%2F&note=for+school",
                        spool=tmp_path)
    assert status == 200
    assert "Your request was sent" in body
    waiting = accessreq.pending(spool=tmp_path)
    assert len(waiting) == 1
    assert waiting[0]["url"] == "https://chinuch.org/"
    assert waiting[0]["note"] == "for school"


def test_a_whitelist_user_with_nothing_allowed_is_given_the_form(get):
    # This is the only place those users can ask for anything: their blocks
    # happen at the firewall, so there is no block page to put a button on.
    status, body = get({"mode": "whitelist", "whitelist": []}, "/")
    assert f'action="{server_mod.REQUEST_PATH}"' in body


def test_sites_that_matched_but_are_not_approved_can_be_asked_for(get):
    # Without this, whitelist mode can only shrink.
    results = [
        {"url": "https://chinuch.org/a", "title": "Lessons", "content": ""},
        {"url": "https://torahanytime.com/b", "title": "Shiurim", "content": ""},
    ]
    status, body = get({"mode": "whitelist", "whitelist": ["chinuch.org"]},
                       "/search?q=torah", results)
    assert "Other sites matched" in body
    assert "torahanytime.com" in body
    # The host, and never the hidden result's own words.
    assert "Shiurim" not in body


def test_a_hidden_site_that_is_also_objectionable_is_not_offered(get):
    results = [{"url": "https://pornhub.com/x", "title": "", "content": ""}]
    status, body = get({"mode": "whitelist", "whitelist": ["chinuch.org"],
                        "blocked_categories": ["adult"]},
                       "/search?q=torah", results)
    assert "pornhub" not in body


def test_a_filtered_user_is_not_offered_sites_to_ask_for(get):
    # They can already open them; the block page is where they would ask.
    results = [{"url": "https://example.com/a", "title": "x", "content": ""}]
    status, body = get({"mode": "filtered"}, "/search?q=x", results)
    assert "Other sites matched" not in body


def test_a_post_to_anything_else_is_a_404(get):
    status, body = post({"mode": "filtered"}, "/nope", "url=x")
    assert status == 404
