"""A standing sweep over the shipped lists, in both directions.

The individual unit tests check the machinery. This checks the LISTS —
the thing that actually decides what a family sees, and the thing most
likely to drift as terms are added. Both directions matter and they pull
against each other, so both are asserted together: a change that improves
one at the other's expense fails here rather than in someone's kitchen.
"""

from pathlib import Path

import pytest

from kosherd import content, search, siterules

ROOT = Path(__file__).parents[2] / "os-image/files/usr/share/kosher"


@pytest.fixture(scope="module")
def scorer():
    return content.load(ROOT / "content-terms.json")


@pytest.fixture(scope="module")
def blocklist():
    return search.load_blocklist(ROOT / "search-blocklist.json")


@pytest.fixture(scope="module")
def sites():
    return siterules.load(ROOT / "site-rules.json")


# Ordinary text a family reads. Every one of these must come back clean;
# each is here because it is the kind of thing naive filters break.
ORDINARY = [
    "Halachos of Shabbos: the 39 melachos and their toldos, with sources.",
    "Yeshiva application: dorm, meals, sedarim, and the rebbeim.",
    "Kosher restaurant guide: dairy, meat, pareve, and hashgacha.",
    "Chicken cutlets, brisket, kugel and cholent for shabbos.",
    "Grilled chicken breast recipe. Season the breasts, sear, rest and slice.",
    "Breast cancer screening: a mammogram examines breast tissue.",
    "Women's health: mammogram, pap smear, and annual checkup guidance.",
    "Breastfeeding support group and lactation consultant near me.",
    "Pediatrician visit: growth chart, vaccines, and a physical exam.",
    "Anatomy class: the skeletal system, muscles of the thigh and calf.",
    "Human reproduction: sexual reproduction, chromosomes determine sex.",
    "Sussex, Middlesex and Essex county records, a sextant and an assessment.",
    "Modest clothing: long skirts, sleeves below the elbow, high necklines.",
    "Women's Clothing: dresses, tops, sweaters, coats, shoes, accessories.",
    "Buy a modest swim skirt and long sleeve swim top for the pool.",
    "Laundry: how to wash delicates, bras and knits without damage.",
    "Home decor: bedroom furniture, mattress sizes, and bedding sets.",
    "Fitness: leg day, squats, and stretching your hamstrings.",
    "Wedding planning: the chuppah, the badeken, and seating charts.",
    "Struggling with pornography addiction? Our counselors can help.",
    "A review of the best porn blocker apps and parental control filters.",
    "A shiur on shmiras einayim in the age of the internet.",
]

# Pages that must not get through, one per shape the web actually takes.
MUST_CATCH = [
    ("an explicit tube site", content.NSFW,
     "Free porn tube: watch xxx videos, hardcore scenes and live sex cams."),
    ("a cam site", content.NSFW,
     "Live cam girls online now. Chat with camgirls, private shows, nude models."),
    ("an escort directory", content.NSFW,
     "Escorts near you. Independent escort service, incall and outcall."),
    ("a leaked-nudes aggregator", content.NSFW,
     "Leaked onlyfans nudes and free nude pics from your favourite creators."),
    ("an AI undressing tool", content.NSFW,
     "Nudify any photo with AI. Undress ai, deepfake nude generator, free."),
    ("a hentai board", content.NSFW,
     "Hentai and rule34 doujin gallery, ecchi art, uncensored."),
    ("a hookup app", content.SUGGESTIVE,
     "Casual sex tonight. Meet singles for a one night stand, no strings."),
    ("a lingerie catalogue", content.SUGGESTIVE,
     "Sexy lingerie: babydoll, negligee, bustier, corset and garter sets."),
    ("a swimwear shop", content.IMMODEST,
     "Bikini and swimsuit collection, monokini, tankini, beachwear."),
    ("an immodest fashion page", content.IMMODEST,
     "Bodycon dresses, crop tops, mini skirts and backless bodysuits."),
]


@pytest.mark.parametrize("text", ORDINARY, ids=range(len(ORDINARY)))
def test_ordinary_text_is_never_convicted(scorer, text):
    verdict = scorer.score(text)
    assert verdict.level == content.CLEAN, f"{verdict} for {text!r}"


@pytest.mark.parametrize("name,least,text", MUST_CATCH,
                         ids=[n for n, _, _ in MUST_CATCH])
def test_what_must_be_caught_is_caught(scorer, name, least, text):
    verdict = scorer.score(text)
    assert verdict.at_least(least), f"{name}: {verdict}"


def test_plurals_do_not_need_their_own_entries(scorer):
    # A list written in the singular missed "crop tops" and "mini skirts",
    # and a page of them scored twenty points and passed.
    for singular, plural in [("crop top", "crop tops"),
                             ("mini skirt", "mini skirts"),
                             ("sex video", "sex videos")]:
        assert scorer.score(plural).points >= scorer.score(singular).points


ORDINARY_QUERIES = [
    "kosher recipes for pesach", "modest swimwear", "swim skirt",
    "nursing bra", "mastectomy bra", "breast pump", "anatomy diagram",
    "sex education laws", "bikini atoll history", "sextant navigation",
    "pornography addiction help", "how to block porn on my phone",
    "porn filter software", "shidduch resume template",
]
EXPLICIT_QUERIES = [
    "porn", "free porn videos", "p0rn", "p-o-r-n", "x n x x", "naked women",
    "onlyfans leaks", "hentai", "best strip club near me", "how to nudify photos",
]


@pytest.mark.parametrize("query", ORDINARY_QUERIES)
def test_an_ordinary_search_runs(blocklist, query):
    assert not (blocklist.contains_any(query)
                and not search.HELP_CONTEXT.search(query)), query


@pytest.mark.parametrize("query", EXPLICIT_QUERIES)
def test_an_explicit_search_does_not(blocklist, query):
    assert blocklist.contains_any(query), query


SHOP_OK = [
    "https://www.amazon.com/s?k=laptop",
    "https://www.amazon.com/s?k=shabbos+candles",
    "https://www.amazon.com/b/socks-hosiery",
    "https://www.target.com/c/books/-/N-5xt",
    "https://www.walmart.com/browse/food/bread/976759",
    "https://chinuch.org/lingerie-of-the-mishkan",
]
SHOP_BLOCKED = [
    "https://www.amazon.com/s?k=x&i=fashion-womens",
    "https://www.amazon.com/b/womens-lingerie/ref=x",
    "https://www.target.com/c/swimwear-women-s-clothing/-/N-5xtcl",
    "https://www.ebay.com/b/Bikinis/63862/bn_123",
]


@pytest.mark.parametrize("url", SHOP_OK)
def test_an_ordinary_shop_page_is_reachable(sites, url):
    assert sites.reason(url) is None, sites.reason(url)


@pytest.mark.parametrize("url", SHOP_BLOCKED)
def test_a_department_page_is_not(sites, url):
    assert sites.reason(url) is not None, url
