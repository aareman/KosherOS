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


def test_subdomains_count(rules):
    assert rules.reason("https://smile.amazon.com/b/womens-lingerie") is not None
