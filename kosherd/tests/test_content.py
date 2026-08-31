"""Judging a page by its words.

Two failure modes matter and they pull against each other: letting an
explicit page through, and blocking a chicken recipe. Most of these tests
are about the second one, because that is the failure a family actually
notices, and the one that gets a filter switched off.
"""

from pathlib import Path

import pytest

from kosherd import content

TERMS = Path(__file__).parents[2] / "os-image/files/usr/share/kosher/content-terms.json"


@pytest.fixture(scope="module")
def scorer():
    return content.load(TERMS)


CLEAN_PAGES = [
    ("a chicken recipe",
     "Grilled chicken breast recipe. Season the breast, sear the chicken "
     "breast, rest the breast and slice. Serve with the legs and thighs."),
    ("a shiur",
     "Parshas Vayeira: hachnasas orchim, Avraham and the three visitors, "
     "and what the meforshim say about chesed."),
    ("a biology lesson",
     "Human reproduction: the biology of sexual reproduction, how "
     "chromosomes determine sex, and the reproductive system."),
    ("breast cancer information",
     "Breast cancer screening: a mammogram examines breast tissue. Talk to "
     "your doctor about breast health and family history."),
    ("a clothing department",
     "Women's clothing: dresses, tops, sweaters, coats, shoes and "
     "accessories. Free returns within thirty days."),
    ("a page about modest dress",
     "Modest clothing for women: long skirts, sleeves below the elbow and "
     "high necklines. No miniskirt, no crop top."),
    ("an addiction helpline",
     "Struggling with pornography addiction? Our counselors and support "
     "group can help you recover."),
    ("a review of filtering software",
     "A review of the best porn blocker apps and parental control filters "
     "for families."),
    ("a shiur on shmiras einayim",
     "A shiur on shmiras einayim in the age of the internet, and how to "
     "fight the yetzer hara."),
]


@pytest.mark.parametrize("name,text", CLEAN_PAGES, ids=[n for n, _ in CLEAN_PAGES])
def test_ordinary_pages_are_clean(scorer, name, text):
    verdict = scorer.score(text)
    assert verdict.level == content.CLEAN, f"{name}: {verdict}"


def test_an_explicit_page_is_nsfw(scorer):
    verdict = scorer.score(
        "Free porn videos, xxx hardcore movies, live sex cams and nude "
        "photos updated daily.")
    assert verdict.level == content.NSFW


def test_help_context_cannot_be_used_as_a_shield(scorer):
    # Sprinkling "recover" over an explicit page must not clear it: the
    # allowance doubles the evidence needed, and explicit pages score far
    # past that.
    verdict = scorer.score(
        "Recover your free porn videos here: xxx hardcore live sex cams, "
        "nude pics, onlyfans, camgirl and chaturbate.")
    assert verdict.level == content.NSFW


def test_swimwear_is_immodest_but_never_explicit(scorer):
    # The bug this guards: with one flat score, four words about swimwear
    # added up to the same verdict as one word about pornography.
    verdict = scorer.score(
        "Bikini and swimsuit collection. Tankini, monokini and beachwear "
        "for summer. Swimsuit edition photos.")
    assert verdict.level == content.IMMODEST


def test_a_lingerie_catalogue_is_suggestive_not_explicit(scorer):
    verdict = scorer.score(
        "Lingerie sale: bras, panties, thongs, bralettes, corsets and "
        "intimate apparel. Sexy babydoll and negligee sets.")
    assert verdict.level == content.SUGGESTIVE


def test_a_dating_site_is_suggestive(scorer):
    verdict = scorer.score(
        "Meet singles near you. Find a date tonight and flirt with local "
        "matches on the best dating site.")
    assert verdict.at_least(content.SUGGESTIVE)


def test_a_repeated_word_cannot_convict_alone(scorer):
    # A word in a navigation menu appears on every page of a site.
    once = scorer.score("bikini").points
    many = scorer.score(" ".join(["bikini"] * 40)).points
    assert many < once * 3
    assert scorer.score(" ".join(["bikini"] * 40)).level != content.NSFW


def test_phrases_beat_their_parts(scorer):
    # "sex video" must score as the phrase, not as the weak word "sex".
    assert scorer.score("sex video").at_least(content.NSFW)
    assert scorer.score("sex").level == content.CLEAN


def test_terms_match_whole_words_only(scorer):
    for innocent in ("Sussex", "Middlesex", "essex", "sextant", "bracelet",
                     "breastfeeding class", "assessment"):
        assert scorer.score(innocent).level == content.CLEAN, innocent


def test_severity_ordering():
    verdict = content.Verdict(content.SUGGESTIVE, 30, ())
    assert verdict.at_least(content.IMMODEST)
    assert verdict.at_least(content.SUGGESTIVE)
    assert not verdict.at_least(content.NSFW)


def test_an_empty_scorer_says_nothing():
    assert content.Scorer().score("porn").level == content.CLEAN
    assert content.load(Path("/nonexistent.json")).score("x").level == content.CLEAN


# -- HTML ---------------------------------------------------------------------

def test_html_is_judged_by_its_visible_text_and_description(scorer):
    html = ('<html><head><title>Free XXX</title>'
            '<meta name="description" content="live sex cams and porn videos">'
            '<style>.porn{color:red}</style></head>'
            '<body><p>Welcome.</p></body></html>')
    assert content.score_html(html, scorer).level == content.NSFW


def test_markup_alone_never_convicts_a_page(scorer):
    # Class names, tracking URLs and script bodies are not what a reader
    # sees, and judging them blocks pages for reasons nobody can explain.
    html = ('<html><body class="sex porn xxx">'
            '<a href="https://ads.example/?tag=porn&x=xxx">Recipes</a>'
            '<script>var porn_ads = "live sex cams nude photos";</script>'
            '<!-- porn xxx hardcore -->'
            '<p>Chicken soup with kneidlach.</p></body></html>')
    assert content.score_html(html, scorer).level == content.CLEAN


def test_the_scan_only_reads_the_top_of_a_huge_page(scorer):
    html = "<html><body>" + ("filler " * 100_000) + "porn xxx live sex</body></html>"
    # Not a verdict either way — the point is that it returns promptly and
    # does not score megabytes of text.
    assert len(content.visible_text(html)) <= 200_100
