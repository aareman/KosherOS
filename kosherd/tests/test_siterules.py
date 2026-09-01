"""Departments on big shops, read from the address.

The two things that matter: a department page on a site the family uses
is caught, and an ordinary page on the same site is not. The second is
what makes this usable — a rule that blocks Amazon is not a rule, it is
an outage.
"""

from pathlib import Path

import pytest

from kosherd import siterules

RULES = Path(__file__).parents[2] / "os-image/files/usr/share/kosher/site-rules.json"


@pytest.fixture(scope="module")
def rules():
    return siterules.load(RULES)


BLOCKED = [
    ("an Amazon department search", "https://www.amazon.com/s?k=shoes&i=fashion-womens"),
    ("an Amazon listing page", "https://www.amazon.com/b/womens-lingerie/ref=x"),
    ("a Target category", "https://www.target.com/c/swimwear-women-s-clothing/-/N-5xtcl"),
    ("a Walmart browse path", "https://www.walmart.com/browse/clothing/womens-sleepwear/1234"),
    ("an eBay category", "https://www.ebay.com/b/Bikinis/63862/bn_123"),
    ("an ASOS section", "https://www.asos.com/women/lingerie-nightwear/cat/"),
    ("a shop nobody wrote rules for",
     "https://smallshop.example/collections/lingerie"),
]

ALLOWED = [
    ("an ordinary Amazon search", "https://www.amazon.com/s?k=laptop"),
    ("an Amazon product page", "https://www.amazon.com/dp/B01234?th=1"),
    ("a Target category that is not one", "https://www.target.com/c/books/-/N-5xt"),
    ("a Walmart grocery path", "https://www.walmart.com/browse/food/bread/976759"),
    ("a frum site whose article title contains the word",
     "https://chinuch.org/lingerie-of-the-mishkan"),
    ("a news article about the industry",
     "https://news.example/2026/09/the-lingerie-industry-in-decline"),
    ("a restaurant whose name contains a term",
     "https://www.brasserie-paris.com/menu"),
    ("a site with no path at all", "https://www.amazon.com/"),
]


@pytest.mark.parametrize("name,url", BLOCKED, ids=[n for n, _ in BLOCKED])
def test_departments_are_caught(rules, name, url):
    assert rules.reason(url) is not None, name


@pytest.mark.parametrize("name,url", ALLOWED, ids=[n for n, _ in ALLOWED])
def test_ordinary_pages_on_the_same_sites_are_not(rules, name, url):
    assert rules.reason(url) is None, f"{name}: {rules.reason(url)}"


def test_the_reason_names_the_thing_that_matched(rules):
    # An admin has to be able to disagree with a specific word, which
    # means seeing which word it was.
    reason = rules.reason("https://www.amazon.com/s?k=x&i=fashion-womens")
    assert "fashion-womens" in reason and "amazon.com" in reason


def test_a_site_with_rules_is_not_also_judged_by_the_generic_list(rules):
    # Otherwise every written rule is silently widened by the fallback,
    # and tuning one site changes the behaviour of all of them.
    assert rules.reason("https://www.amazon.com/gp/browse/adult-toys") is not None
    assert rules.reason("https://www.amazon.com/gp/help/escorts") is None


def test_terms_match_whole_segments(rules):
    assert rules.reason("https://smallshop.example/collections/brasserie") is None
    assert rules.reason("https://smallshop.example/collections/bikinis") is not None


def test_percent_encoded_departments_are_still_read(rules):
    # A department reaches the server encoded as often as not.
    assert rules.reason(
        "https://www.target.com/c/swimwear%2Dwomen-s-clothing/-/N-1") is not None


def test_no_rules_file_means_no_rules():
    assert siterules.load(Path("/nonexistent.json")).reason(
        "https://www.amazon.com/b/womens-lingerie") is None


def test_a_department_people_actually_need_survives(rules):
    # "Socks & Hosiery" is where you buy socks. A term that removes the
    # sock department is an outage, not a filter.
    assert rules.reason("https://www.amazon.com/b/socks-hosiery") is None
    assert rules.reason("https://www.walmart.com/browse/socks-hosiery/1") is None
    # The tights department still says so.
    assert rules.reason("https://www.amazon.com/b/womens-tights") is not None


def test_subdomains_count(rules):
    assert rules.reason("https://smile.amazon.com/b/womens-lingerie") is not None


# -- custom element rules -----------------------------------------------------

def test_a_site_can_override_what_is_stripped_and_from_where():
    # The point of making these data rather than code: a layout the
    # defaults do not reach is a rules-file edit, not a release.
    rules = siterules.SiteRules(sites=[{
        "hosts": ["shop.example"],
        "terms": ["lingerie"],
        "strip_tags": ["div", "a"],
        "strip_terms": ["swim club", "lingerie"],
    }])
    tags, is_blocked, quick = rules.strip_spec("shop.example")
    assert tags == ("div", "a")
    assert is_blocked("Join the Swim Club")
    assert is_blocked("lingerie")
    assert not is_blocked("socks")
    assert quick.search("come to the swim club")


def test_a_site_without_overrides_strips_its_own_department_terms():
    rules = siterules.SiteRules(sites=[{
        "hosts": ["shop.example"], "terms": ["lingerie", "swimwear"]}])
    tags, is_blocked, _quick = rules.strip_spec("shop.example")
    assert tags == siterules.DEFAULT_STRIP_TAGS
    assert is_blocked("Swimwear") and not is_blocked("Shoes")


def test_a_host_with_no_rules_has_nothing_to_strip():
    rules = siterules.SiteRules(sites=[{"hosts": ["shop.example"],
                                        "terms": ["lingerie"]}])
    assert rules.strip_spec("chinuch.org") is None
    assert rules.covers("shop.example") and not rules.covers("chinuch.org")
