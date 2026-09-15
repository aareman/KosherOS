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


def test_the_front_page_is_written_for_the_site_not_lifted_from_the_readme():
    # The readme is full of raw HTML (a centred div, shield badges, a
    # details block) and Python-Markdown does not render Markdown inside
    # raw HTML, so lifting it made the front page show its own source.
    index = (ROOT / "docs/index.md").read_text()
    assert "<div align=" not in index and "<details>" not in index
    assert "[ci-shield]" not in index, "reference-style badges never resolve here"
    assert index.startswith("# KosherOS")
    generated = {"index.md", "releases.md", "third-party.md"}
    for target in set(re.findall(r"\]\(([a-z-]+\.md)\)", index)) - generated:
        assert (ROOT / "docs" / target).is_file(), target
    for image in set(re.findall(r"\]\((images/[^)]+)\)", index)):
        assert (ROOT / "docs" / image).is_file(), image


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
    assert "```mermaid" in (ROOT / "docs/index.md").read_text()
