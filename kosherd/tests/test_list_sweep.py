"""The sweep over real pages (scripts/list-sweep.py), minus the network.

CI runs it against the web; this checks that what it does to a page is
what the proxy does, that its verdicts follow its thresholds, that a
runner without network cannot make the lists look wrong, and that no
adult domain can reach the summary.
"""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from kosherd import content

ROOT = Path(__file__).parents[2]
SPEC = importlib.util.spec_from_file_location("list_sweep", ROOT / "scripts" / "list-sweep.py")
sweep = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = sweep      # dataclasses resolve annotations through it
SPEC.loader.exec_module(sweep)


@pytest.fixture(scope="module")
def judge():
    return sweep.Judge()


def test_a_page_is_judged_as_the_proxy_judges_it(judge):
    html = ("<html><head><title>Free porn tube</title></head><body>"
            "<p>xxx videos, hardcore scenes and live sex cams. What the fuck.</p>"
            "<script>var x = 'bikini bikini bikini';</script></body></html>")
    page = sweep.Page("adult", "", "https://example.invalid/")
    judge.judge(page, html)
    assert page.level == content.NSFW
    assert "fuck" in page.words
    assert "bikini" not in page.hits, "script contents are not visible text"


def test_an_ordinary_page_in_hebrew_is_clean(judge):
    html = "<html><body><h1>מתכון לחלה</h1><p>קמח, שמרים, מים, סוכר ומלח. מזונות לשבת.</p></body></html>"
    page = sweep.Page("curated", "he", "https://example.invalid/")
    judge.judge(page, html)
    assert page.level == content.CLEAN and page.words == []


def _pages(group, lang, judged, level="clean", words=()):
    out = []
    for i in range(judged):
        p = sweep.Page(group, lang, f"https://site{i}.invalid/", status="ok", level=level,
                       words=list(words))
        out.append(p)
    return out


def test_too_few_pages_means_no_verdict_not_a_failure():
    # A runner without network fetches nothing; that says nothing about the lists.
    pages = [sweep.Page("curated", "he", "https://x.invalid/", status="URLError")]
    results = sweep.checks(pages)
    assert all(c.verdict == "no verdict" for c in results)
    assert not any(c.failed for c in results)


def test_the_thresholds_fail_what_they_should():
    clean = _pages("curated", "en", 40)
    assert not any(c.failed for c in sweep.checks(clean))
    # A tenth of the curated pages reading as explicit is over the limit.
    bad = _pages("curated", "en", 36) + _pages("curated", "en", 4, level=content.NSFW)
    assert any(c.failed and "explicit" in c.name for c in sweep.checks(bad))
    # Most of a language's pages being rewritten is a list of ordinary words.
    rewriting = _pages("curated", "nl", 12) + _pages("curated", "nl", 14, words=["af"])
    assert any(c.failed and "nl" in c.name for c in sweep.checks(rewriting))
    # Adult pages the scorer misses.
    missed = _pages("adult", "", 30) + _pages("adult", "", 10, level=content.NSFW)
    assert any(c.failed and "adult" in c.name for c in sweep.checks(missed))
    caught = _pages("adult", "", 5) + _pages("adult", "", 35, level=content.NSFW)
    assert not any(c.failed for c in sweep.checks(caught))


def test_the_summary_never_names_an_adult_domain():
    pages = _pages("adult", "", 25, level=content.NSFW) + [
        sweep.Page("adult", "", "https://never-in-a-log.invalid/", status="ok", level="clean"),
        sweep.Page("curated", "fr", "https://ordinary-news.invalid/", status="ok",
                   level=content.IMMODEST, points=12, hits=["bikini"]),
    ]
    report = sweep.summary(pages, sweep.checks(pages), 1.0)
    assert "never-in-a-log" not in report and "site0" not in report
    assert "ordinary-news.invalid" in report, "ordinary sites are named, so a person can look"
    assert "1 did not" in report


def test_the_curated_sites_cover_every_language():
    sites = json.loads(sweep.CURATED_PATH.read_text())
    languages = {p.stem for p in (ROOT / "os-image/lists").glob("*.json") if p.stem != "headers"}
    assert set(sites) == languages
    for lang, urls in sites.items():
        assert len(urls) >= 4, lang
        assert all(u.startswith("https://") for u in urls), lang


def test_legacy_charsets_are_decoded():
    hebrew = "<html><head><meta charset=windows-1255></head><body>שלום</body></html>"
    assert "שלום" in sweep.decode(hebrew.encode("windows-1255"), "text/html")
    assert "שלום" in sweep.decode(hebrew.encode("windows-1255"), "text/html; charset=windows-1255")
    assert "שלום" in sweep.decode("<p>שלום</p>".encode(), "text/html")
