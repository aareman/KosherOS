"""Search result filtering.

The point of this is ergonomics as much as safety: a filter that only
blocks pages leaves the person guessing which of ten links will work, and
in whitelist mode it leaves the whitelist completely invisible.
"""

import json
from pathlib import Path

import pytest

from kosherd import content, language, search
from kosherd.policy import Policy, UserPolicy

ROOT = Path(__file__).parents[2]
TERMS = Path(__file__).parents[2] / "os-image/files/usr/share/kosher/content-terms.json"
BLOCKLIST = Path(__file__).parents[2] / "os-image/files/usr/share/kosher/search-blocklist.json"
WORDLIST = Path(__file__).parents[2] / "os-image/files/usr/share/kosher/wordlist.json"


class FakeBundle:
    """A category database with a handful of known hosts."""

    def __init__(self, hosts=None):
        self.hosts = hosts or {"pornhub.com": {"adult"}, "bet365.com": {"gambling"},
                               "facebook.com": {"social"}, "chinuch.org": {"education"}}

    def categories_of(self, host):
        for known, cats in self.hosts.items():
            if host == known or host.endswith("." + known):
                return set(cats)
        return set()

    def blocked_categories_of(self, host, blocked):
        return self.categories_of(host) & set(blocked)


def make_filter(users, **kw):
    policy = search.SearchPolicy(Path("/nonexistent"))
    policy._users = users
    policy._mtime = 0  # do not go looking for the file
    policy._refresh = lambda: None
    kw.setdefault("bundle", FakeBundle())
    kw.setdefault("scorer", content.load(TERMS))
    kw.setdefault("blocklist", search.load_blocklist(BLOCKLIST))
    kw.setdefault("wordlist", language.load(WORDLIST))
    return search.ResultFilter(policy=policy, **kw)


# -- who is asking ------------------------------------------------------------

def test_an_unknown_user_gets_nothing():
    # The same fail-closed rule the firewall uses for unknown UIDs: if we
    # cannot tell who is searching, we cannot tell what they may see.
    f = make_filter({})
    assert not f.allows(4242, "https://chinuch.org/")
    assert not f.allows(None, "https://chinuch.org/")


def test_an_unfiltered_user_gets_everything():
    f = make_filter({1001: {"mode": "unfiltered"}})
    assert f.allows(1001, "https://pornhub.com/")
    assert f.query_reason(1001, "porn") is None


def test_a_user_with_no_internet_gets_nothing():
    f = make_filter({1001: {"mode": "none"}})
    assert not f.allows(1001, "https://chinuch.org/")


# -- whitelist ----------------------------------------------------------------

def test_whitelist_mode_shows_only_whitelisted_sites():
    f = make_filter({1001: {"mode": "whitelist",
                            "whitelist": ["chinuch.org", "torahanytime.com"]}})
    assert f.allows(1001, "https://chinuch.org/lessons")
    assert f.allows(1001, "https://www.chinuch.org/")       # subdomain
    assert not f.allows(1001, "https://example.com/")


def test_whitelist_search_makes_the_whitelist_discoverable():
    # The reason this feature exists: a whitelist user searching should get
    # back the sites they can actually open, not a page of dead links.
    f = make_filter({1001: {"mode": "whitelist", "whitelist": ["chinuch.org"]}})
    results = [{"url": "https://chinuch.org/a", "title": "Lessons"},
               {"url": "https://example.com/b", "title": "Something else"},
               {"url": "https://chinuch.org/c", "title": "More lessons"}]
    kept = f.filter_results(1001, results, deep=False)
    assert [r["url"] for r in kept] == ["https://chinuch.org/a", "https://chinuch.org/c"]


def test_a_whitelisted_site_is_not_second_guessed_by_its_words():
    # Somebody approved this site by name; re-judging it by a word scan
    # would quietly override them.
    f = make_filter({1001: {"mode": "whitelist", "whitelist": ["example.org"]}})
    assert f.scan_reason(1001, "https://example.org/",
                         content.Verdict(content.NSFW, 99, ())) is None


# -- categories and rules -----------------------------------------------------

def test_blocked_categories_are_dropped():
    f = make_filter({1001: {"mode": "dnsfilter",
                            "blocked_categories": ["adult", "gambling"]}})
    assert not f.allows(1001, "https://pornhub.com/")
    assert not f.allows(1001, "https://www.bet365.com/en")
    assert f.allows(1001, "https://chinuch.org/")
    assert f.allows(1001, "https://facebook.com/")  # social is not blocked here


def test_url_rules_apply_in_filtered_mode():
    f = make_filter({1001: {"mode": "filtered",
                            "rules": [{"action": "block",
                                       "pattern": "youtube.com/shorts*"}]}})
    assert not f.allows(1001, "https://youtube.com/shorts/abc")
    assert f.allows(1001, "https://youtube.com/watch?v=abc")


def test_an_unreadable_rule_closes_rather_than_opens():
    f = make_filter({1001: {"mode": "filtered",
                            "rules": [{"action": "explode", "pattern": "x"}]}})
    assert not f.allows(1001, "https://example.com/")


# -- the query itself ---------------------------------------------------------

def test_an_explicit_query_does_not_run_at_all():
    # Dropping every result is not enough: the snippets on the page are
    # written by the engine from the pages it found.
    f = make_filter({1001: {"mode": "filtered"}})
    assert f.query_reason(1001, "free porn videos") is not None
    assert f.query_reason(1001, "kosher recipes for pesach") is None


def test_asking_for_help_is_not_an_explicit_query():
    f = make_filter({1001: {"mode": "filtered"}})
    assert f.query_reason(1001, "pornography addiction help") is None
    assert f.query_reason(1001, "how to block porn on my phone") is None


def test_profanity_in_a_query_is_blocked_only_when_asked_for():
    f = make_filter({1001: {"mode": "filtered", "language_filter": "block"}})
    off = make_filter({1002: {"mode": "filtered"}})
    word = next(iter(language.load(WORDLIST).replacements), None)
    assert word, "the shipped word list is empty"
    assert f.query_reason(1001, f"why is {word} bad") is not None
    assert off.query_reason(1002, f"why is {word} bad") is None


# -- content ------------------------------------------------------------------

def test_a_result_whose_snippet_reads_as_explicit_is_dropped():
    # The host is unknown to every list; the words are not.
    f = make_filter({1001: {"mode": "dnsfilter", "blocked_categories": ["adult"]}})
    assert not f.allows(1001, "https://brand-new-domain.example/",
                        text="Free XXX porn videos and live sex cams")
    assert f.allows(1001, "https://brand-new-domain.example/",
                    text="Chicken soup recipe with kneidlach")


def test_the_media_level_decides_how_much_a_page_may_say():
    strict = make_filter({1001: {"mode": "filtered", "media_level": "immodest"}})
    relaxed = make_filter({1002: {"mode": "filtered", "media_level": "none"}})
    snippet = ("Bikini and swimsuit collection. Tankini, monokini and "
               "beachwear for summer. Swimsuit edition photos.")
    assert not strict.allows(1001, "https://shop.example/", text=snippet)
    assert relaxed.allows(1002, "https://shop.example/", text=snippet)


def test_image_results_are_withheld_from_users_with_media_filtering():
    strict = make_filter({1001: {"mode": "filtered", "media_level": "suggestive"}})
    relaxed = make_filter({1002: {"mode": "filtered", "media_level": "none"}})
    assert not strict.allows(1001, "https://chinuch.org/pic.jpg", is_media=True)
    assert relaxed.allows(1002, "https://chinuch.org/pic.jpg", is_media=True)


# -- the deep scan ------------------------------------------------------------

def test_only_hosts_no_list_knows_about_are_worth_reading():
    f = make_filter({1001: {"mode": "filtered"}})
    assert not f.unknown_host("https://pornhub.com/x")   # already judged
    assert not f.unknown_host("https://chinuch.org/x")
    assert f.unknown_host("https://brand-new-domain.example/x")


def test_the_deep_scan_drops_a_page_the_lists_never_heard_of():
    pages = {
        "https://newsite.example/": "<title>Free XXX</title><p>live sex cams "
                                    "and porn videos, nude photos daily</p>",
        "https://kosherblog.example/": "<title>Recipes</title><p>Chicken soup</p>",
    }
    scanner = _scanner(pages)
    f = make_filter({1001: {"mode": "filtered", "media_level": "none"}},
                    scanner=scanner)
    results = [{"url": u, "title": "", "content": ""} for u in pages]
    kept = [r["url"] for r in f.filter_results(1001, results)]
    assert kept == ["https://kosherblog.example/"]


def test_a_page_that_cannot_be_read_is_left_alone():
    # Failing to fetch is not evidence. Dropping every slow or offline site
    # would make search useless on a bad connection.
    def boom(url):
        raise OSError("no route to host")

    f = make_filter({1001: {"mode": "filtered"}}, scanner=_scanner({}, fetch=boom))
    results = [{"url": "https://unreachable.example/", "title": "", "content": ""}]
    assert len(f.filter_results(1001, results)) == 1


def test_the_deep_scan_is_capped_per_search():
    fetched = []

    def record(url):
        fetched.append(url)
        return "<p>Chicken soup</p>"

    f = make_filter({1001: {"mode": "filtered"}}, scanner=_scanner({}, fetch=record))
    results = [{"url": f"https://site{i}.example/", "title": "", "content": ""}
               for i in range(50)]
    f.filter_results(1001, results, max_scans=4)
    assert len(fetched) == 4


def test_verdicts_are_cached_so_the_same_host_is_read_once(tmp_path):
    calls = []

    def once(url):
        calls.append(url)
        return "<p>Chicken soup</p>"

    from kosherd import pagescan
    scanner = pagescan.PageScanner(
        scorer=content.load(TERMS), fetch=once,
        cache=pagescan.VerdictCache(tmp_path / "v.sqlite"))
    scanner.verdict("https://a.example/1", "a.example")
    scanner.verdict("https://a.example/2", "a.example")
    assert len(calls) == 1


def test_an_expired_verdict_is_read_again(tmp_path):
    from kosherd import pagescan
    cache = pagescan.VerdictCache(tmp_path / "v.sqlite", ttl=-1)
    cache.put("a.example", content.Verdict(content.NSFW, 99, ()))
    assert cache.get("a.example") is None


def _scanner(pages, fetch=None):
    from kosherd import pagescan

    class MemoryCache(pagescan.VerdictCache):
        def __init__(self):
            self._store = {}

        def get(self, host):
            return self._store.get(host)

        def put(self, host, verdict):
            self._store[host] = verdict

    return pagescan.PageScanner(
        scorer=content.load(TERMS),
        cache=MemoryCache(),
        fetch=fetch or (lambda url: pages.get(url, "")),
    )


# -- what the daemon hands the service ----------------------------------------

def test_render_policy_gives_the_service_only_what_it_filters_with():
    policy = Policy(revision=3, users=[
        UserPolicy(uid=1001, username="a", mode="whitelist",
                   whitelist=["chinuch.org"]),
        UserPolicy(uid=1002, username="b", mode="filtered",
                   blocked_categories=["adult"], media_level="immodest"),
    ])
    doc = json.loads(search.render_policy(policy))
    assert set(doc) == {"1001", "1002"}
    assert doc["1001"]["whitelist"] == ["chinuch.org"]
    assert doc["1002"]["media_level"] == "immodest"
    for entry in doc.values():
        assert set(entry) == {"mode", "whitelist", "blocked_categories",
                              "rules", "media_level", "language_filter"}


def test_the_policy_file_is_reread_when_it_changes(tmp_path):
    path = tmp_path / "policy.json"
    path.write_text(json.dumps({"1001": {"mode": "none"}}))
    policy = search.SearchPolicy(path)
    assert policy.for_uid(1001)["mode"] == "none"

    import os
    import time
    path.write_text(json.dumps({"1001": {"mode": "unfiltered"}}))
    os.utime(path, (time.time() + 10, time.time() + 10))
    assert policy.for_uid(1001)["mode"] == "unfiltered"


def test_a_missing_policy_file_means_nobody_may_search():
    policy = search.SearchPolicy(Path("/nonexistent/policy.json"))
    assert policy.for_uid(1001) == search.UNKNOWN


# -- shop departments ---------------------------------------------------------

def test_a_result_leading_into_a_blocked_department_is_dropped():
    # A result that leads where a click would be blocked is a result that
    # leads to a block page, which is the thing this whole feature exists
    # to prevent.
    from kosherd import siterules
    sites = siterules.load(
        ROOT / "os-image/files/usr/share/kosher/site-rules.json")
    f = make_filter({1001: {"mode": "dnsfilter",
                            "blocked_categories": ["immodest"]}}, sites=sites)
    assert not f.allows(1001, "https://www.amazon.com/s?k=x&i=fashion-womens")
    assert f.allows(1001, "https://www.amazon.com/s?k=laptop")


def test_departments_are_only_filtered_for_accounts_that_asked():
    from kosherd import siterules
    sites = siterules.load(
        ROOT / "os-image/files/usr/share/kosher/site-rules.json")
    f = make_filter({1001: {"mode": "dnsfilter",
                            "blocked_categories": ["gambling"]}}, sites=sites)
    assert f.allows(1001, "https://www.amazon.com/s?k=x&i=fashion-womens")
