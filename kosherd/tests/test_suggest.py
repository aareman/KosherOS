"""Autocomplete, and a site's own search box.

Autocomplete is worse than the results: it puts the words on screen
unprompted, while somebody is typing something else. And blocking the
lingerie department is worth nothing if the search box on the same page
reaches it anyway.
"""

import json
from pathlib import Path

import pytest

from kosherd import siterules, suggest

RULES = Path(__file__).parents[2] / "os-image/files/usr/share/kosher/site-rules.json"


@pytest.fixture(scope="module")
def rules():
    return siterules.load(RULES)


def blocked(text):
    return "lingerie" in text.lower() or "bikini" in text.lower()


# -- pruning whatever shape came back -----------------------------------------

def test_a_bare_list_of_strings():
    body = json.dumps(["laptop", "lingerie set", "lamp"]).encode()
    assert json.loads(suggest.filter_json(body, blocked)) == ["laptop", "lamp"]


def test_amazons_shape():
    body = json.dumps({"suggestions": [
        {"value": "laptop stand", "refTag": "x"},
        {"value": "lingerie", "refTag": "y"},
    ]}).encode()
    out = json.loads(suggest.filter_json(body, blocked))
    assert [s["value"] for s in out["suggestions"]] == ["laptop stand"]


def test_ebays_shape():
    body = json.dumps({"res": {"sug": ["bike", "bikini top", "bin"]}}).encode()
    out = json.loads(suggest.filter_json(body, blocked))
    assert out["res"]["sug"] == ["bike", "bin"]


def test_an_entry_is_dropped_for_text_anywhere_inside_it():
    # The word is not always in the field a schema would call the label.
    body = json.dumps([{"id": 1, "meta": {"category": "Lingerie"}, "t": "x"}]).encode()
    assert json.loads(suggest.filter_json(body, blocked)) == []


def test_a_clean_response_is_left_exactly_alone():
    body = json.dumps({"suggestions": [{"value": "laptop"}]}).encode()
    assert suggest.filter_json(body, blocked) is None


def test_the_structure_around_the_list_survives():
    # Breaking the envelope breaks the page; only list entries may go.
    body = json.dumps({"prefix": "li", "suggestions": ["lingerie"],
                       "alias": "aps", "version": 3}).encode()
    out = json.loads(suggest.filter_json(body, blocked))
    assert out == {"prefix": "li", "suggestions": [], "alias": "aps",
                   "version": 3}


def test_something_that_is_not_json_is_left_alone():
    assert suggest.filter_json(b"<html>not json</html>", blocked) is None
    assert suggest.filter_json(b"", blocked) is None


def test_an_enormous_body_is_not_a_suggestion_response():
    big = json.dumps(["lingerie"] * 200_000).encode()
    assert len(big) > suggest.MAX_BYTES
    assert suggest.filter_json(big, blocked) is None


def test_deeply_nested_json_terminates():
    node = "lingerie"
    for _ in range(200):
        node = [node]
    suggest.prune(node, blocked)  # must return rather than recurse forever


# -- finding the search box and the autocomplete endpoint ---------------------

def test_a_search_on_a_shop_is_recognised(rules):
    assert rules.search_text("https://www.amazon.com/s?k=laptop") == "laptop"
    assert rules.search_text("https://www.ebay.com/sch/i.html?_nkw=sefer") == "sefer"
    assert rules.search_text("https://www.amazon.com/gp/cart") is None
    assert rules.search_text("https://chinuch.org/?q=parsha") is None


def test_a_department_search_is_blocked(rules):
    assert rules.blocked_search("https://www.amazon.com/s?k=lingerie")
    assert rules.blocked_search("https://www.amazon.com/s?k=womens+swimwear")
    assert rules.blocked_search("https://www.ebay.com/sch/i.html?_nkw=bikini")


def test_an_ordinary_search_is_not(rules):
    for url in ("https://www.amazon.com/s?k=laptop+stand",
                "https://www.amazon.com/s?k=shabbos+candles",
                "https://www.amazon.com/s?k=brasserie+cookbook",
                "https://www.ebay.com/sch/i.html?_nkw=sefer+torah"):
        assert rules.blocked_search(url) is None, url


def test_autocomplete_endpoints_are_recognised(rules):
    assert rules.is_suggestions(
        "https://completion.amazon.com/api/2017/suggestions?prefix=lin")
    assert rules.is_suggestions("https://autosug.ebay.com/autosug?kwd=bik")
    assert rules.is_suggestions("https://www.walmart.com/typeahead/v2/complete?term=b")


def test_an_ordinary_page_is_not_an_autocomplete_endpoint(rules):
    assert not rules.is_suggestions("https://www.amazon.com/s?k=laptop")
    assert not rules.is_suggestions("https://www.amazon.com/gp/cart/view.html")
    assert not rules.is_suggestions("https://chinuch.org/autocomplete")
