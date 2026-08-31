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
        "uid_for_port": staticmethod(lambda port: UID)})()
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


def test_safe_search_is_forced_on_every_backend_call(get):
    backend = server_mod.Backend("http://127.0.0.1:1")
    captured = {}

    class Fake:
        def __enter__(self_):
            return self_

        def __exit__(self_, *a):
            return False

        def read(self_):
            return b"{}"

    def fake_urlopen(request, timeout=None):
        captured["url"] = request.full_url
        return Fake()

    server_mod.urllib.request.urlopen = fake_urlopen
    try:
        backend.search("torah", "general", 1)
    finally:
        import importlib
        importlib.reload(server_mod)
    assert "safesearch=2" in captured["url"]
