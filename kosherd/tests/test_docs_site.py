"""The docs site's generated pages, and that what it claims is true."""

import importlib.util
import re
import sys
from pathlib import Path

ROOT = Path(__file__).parents[2]
spec = importlib.util.spec_from_file_location("build_docs", ROOT / "scripts/build-docs.py")
bd = importlib.util.module_from_spec(spec)
sys.modules["build_docs"] = bd
spec.loader.exec_module(bd)


def test_the_overview_is_written_for_the_site_not_lifted_from_the_readme():
    # The readme is full of raw HTML (a centred div, shield badges, a
    # details block) and Python-Markdown does not render Markdown inside
    # raw HTML, so lifting it made the page show its own source.
    page = (ROOT / "docs/overview.md").read_text()
    assert "<div align=" not in page and "<details>" not in page
    assert "[ci-shield]" not in page, "reference-style badges never resolve here"
    assert page.startswith("# KosherOS")
    generated = {"index.md", "releases.md", "third-party.md"}
    for target in set(re.findall(r"\]\(([a-z-]+\.md)\)", page)) - generated:
        assert (ROOT / "docs" / target).is_file(), target
    for image in set(re.findall(r"\]\((images/[^)]+)\)", page)):
        assert (ROOT / "docs" / image).is_file(), image


def test_the_front_page_is_the_landing_template_and_everything_it_names_exists():
    index = (ROOT / "docs/index.md").read_text()
    assert "template: home.html" in index
    assert "- navigation" in index and "- toc" in index, "no sidebars on a landing page"
    nav = (ROOT / "mkdocs.yml").read_text()
    assert "custom_dir: overrides" in nav
    assert re.search(r"^\s+- [^:]+: overview\.md\s*$", nav, re.M), "the old front page stays reachable"
    template = (ROOT / "overrides/home.html").read_text()
    assert template.startswith('{% extends "main.html" %}')
    generated = {"releases/", "third-party/"}
    for target in set(re.findall(r"\{\{\s*'([^']+)'\s*\|\s*url\s*\}\}", template)):
        if target in generated:
            continue
        if target.endswith("/"):
            assert (ROOT / "docs" / (target[:-1] + ".md")).is_file(), target
        else:
            assert (ROOT / "docs" / target).is_file(), target
    for asset in ("docs/stylesheets/home.css", "docs/javascripts/home.js", "docs/images/hero.jpg"):
        assert (ROOT / asset).is_file(), asset


def test_the_landing_pages_presets_are_the_shipped_ones():
    # The chooser on the front page draws real presets, not marketing ones:
    # every chip is a row of the overview's preset table, in the same words.
    template = (ROOT / "overrides/home.html").read_text()
    overview = (ROOT / "docs/overview.md").read_text()
    chips = re.findall(r'data-preset="[a-z]+"[^>]*>([^<]+)</button>', template)
    assert len(chips) == 6
    for chip in chips:
        assert f"| **{chip}** |" in overview, chip
    script = (ROOT / "docs/javascripts/home.js").read_text()
    for key in re.findall(r'data-preset="([a-z]+)"', template):
        assert re.search(rf"^\s+{key}: \{{", script, re.M), key


def test_the_landing_page_moves_only_for_people_who_want_motion():
    css = (ROOT / "docs/stylesheets/home.css").read_text()
    js = (ROOT / "docs/javascripts/home.js").read_text()
    assert "prefers-reduced-motion: reduce" in css and "prefers-reduced-motion" in js
    # Nothing is hidden unless the script that will reveal it is running.
    assert ".ko-js .ko-reveal { opacity: 0;" in css
    assert 'classList.add("ko-js")' in (ROOT / "overrides/home.html").read_text()


def test_no_page_on_the_site_hides_markdown_inside_raw_html():
    for page in (ROOT / "docs").glob("*.md"):
        text = page.read_text()
        for opener in ("<div", "<details", "<table"):
            if opener in text:
                # Only allowed with the md_in_html marker, which is what
                # makes the content inside render.
                for line in text.splitlines():
                    if line.strip().startswith(opener):
                        assert "markdown" in line, f"{page.name}: {line.strip()[:60]}"


def test_the_releases_page_lists_every_release_newest_first():
    page = bd.releases_page([
        {"tagName": "v0.1.0-pre.2", "name": "KosherOS 0.1.0-pre.2", "isPrerelease": True,
         "publishedAt": "2026-09-15T02:00:00Z", "isLatest": False, "body": "- Two"},
        {"tagName": "v0.1.0-pre.1", "name": "KosherOS 0.1.0-pre.1", "isPrerelease": False,
         "publishedAt": "2026-09-14T02:00:00Z", "isLatest": True, "body": "- One"},
    ])
    assert page.index("pre.2") < page.index("pre.1")
    assert "## KosherOS 0.1.0-pre.1 · **stable**" in page
    assert "## KosherOS 0.1.0-pre.2 · pre-release" in page
    assert "- Two" in page and "- One" in page


def test_every_page_in_the_nav_exists():
    import tomllib  # noqa: F401  (stdlib presence check; mkdocs.yml is yaml)

    nav = (ROOT / "mkdocs.yml").read_text()
    for target in set(re.findall(r":\s*([a-z-]+\.md)\s*$", nav, re.M)):
        if target in ("index.md", "releases.md", "third-party.md"):
            continue  # generated at build
        assert (ROOT / "docs" / target).is_file(), target


def test_the_supported_page_matches_what_the_image_actually_sets():
    supported = (ROOT / "docs/supported.md").read_text()
    profile = (ROOT / "os-image/files/etc/profile.d/kosher-ca.sh").read_text()
    for var in re.findall(r"`([A-Z_]{6,})[`=]", supported):
        if var in ("JAVA_TOOL_OPTIONS",):
            continue  # named as deliberately not set
        assert var in profile, f"{var} is claimed on the supported page but not set"
    from kosherd.policy import DEVELOPER_REGISTRIES

    for domain in ("registry.npmjs.org", "pypi.org", "rubygems.org", "crates.io",
                   "deno.land", "proxy.golang.org", "astral.sh", "bun.sh"):
        assert domain in supported and domain in DEVELOPER_REGISTRIES, domain


def test_generate_runs_offline(tmp_path, monkeypatch):
    # Without gh the releases page still gets written, from local tags.
    monkeypatch.setattr(bd, "releases_from_github", lambda: None)
    monkeypatch.setattr(bd, "DOCS", tmp_path)
    (tmp_path / "images").mkdir()
    bd.generate()
    assert (tmp_path / "releases.md").read_text().startswith("# Releases")
    assert (tmp_path / "third-party.md").is_file()
    assert not (tmp_path / "index.md").exists(), "the front page is written, not generated"


def test_the_diagram_is_handed_to_the_theme_to_draw():
    nav = (ROOT / "mkdocs.yml").read_text()
    assert "name: mermaid" in nav and "fence_code_format" in nav
    assert "```mermaid" in (ROOT / "docs/overview.md").read_text()


def test_the_releases_page_sorts_by_date_whatever_order_github_gives():
    # GitHub compares the tag as text, which listed pre.9 above pre.13.
    out_of_order = [
        {"tagName": "v0.1.0-pre.9", "name": "old", "publishedAt": "2026-09-15T02:00:00Z",
         "isPrerelease": True, "isLatest": False, "body": ""},
        {"tagName": "v0.1.0-pre.013", "name": "new", "publishedAt": "2026-09-15T15:00:00Z",
         "isPrerelease": True, "isLatest": False, "body": ""},
    ]
    assert [r["name"] for r in bd.newest_first(out_of_order)] == ["new", "old"]
    page = bd.releases_page(out_of_order)
    assert page.index("new") < page.index("old")



def test_the_site_wears_the_products_own_colours():
    # Sampled from branding/wallpaper.png, not a stock Material palette, so
    # the site, the desktop and the boot splash read as one product.
    css = (ROOT / "docs/stylesheets/brand.css").read_text()
    for value in ("#faf7f0", "#48423a", "#8f6a3f", "#1b1813", "#c99a66"):
        assert value in css, value
    for scheme in ('[data-md-color-scheme="default"]', '[data-md-color-scheme="slate"]'):
        assert scheme in css, scheme
    nav = (ROOT / "mkdocs.yml").read_text()
    assert "stylesheets/brand.css" in nav
    assert "primary: custom" in nav and "indigo" not in nav


def test_the_cream_is_the_artworks_cream():
    from PIL import Image

    wallpaper = Image.open(ROOT / "branding/wallpaper.png").convert("RGB")
    wallpaper.thumbnail((80, 80))
    lights = [p for p in wallpaper.getdata() if min(p) > 200]
    assert lights, "the artwork is a light sepia painting"
    # Its paper is warm: more red than blue, which is what #faf7f0 is too.
    average = tuple(sum(c[i] for c in lights) / len(lights) for i in range(3))
    assert average[0] > average[2], "warm, not a cool grey"
