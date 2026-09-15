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


def test_the_front_page_is_the_readme_with_links_that_work_on_the_site():
    index = bd.index_from_readme((ROOT / "readme.md").read_text())
    assert "](docs/" not in index
    assert 'src="docs/' not in index
    assert 'src="images/logo.png"' in index
    # Every local page the front page links to exists in docs/.
    generated = {"index.md", "releases.md", "third-party.md"}
    for target in set(re.findall(r"\]\(([a-z-]+\.md)\)", index)) - generated:
        assert (ROOT / "docs" / target).is_file(), target


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
    # Without gh the page still gets written, from local tags.
    monkeypatch.setattr(bd, "releases_from_github", lambda: None)
    monkeypatch.setattr(bd, "DOCS", tmp_path)
    (tmp_path / "images").mkdir()
    bd.generate()
    assert (tmp_path / "index.md").is_file()
    assert (tmp_path / "releases.md").read_text().startswith("# Releases")
