#!/usr/bin/env python3
"""Assemble the docs site's generated pages, then (optionally) build it.

    scripts/build-docs.py            # write docs/index.md, docs/releases.md, copy the logo
    scripts/build-docs.py --build    # ...and run mkdocs build
    scripts/build-docs.py --serve    # ...and run mkdocs serve

Two pages are generated rather than written:

- `docs/index.md` is the repository readme with its links rewritten for the
  site (`docs/foo.md` -> `foo.md`, `branding/logo.png` -> `images/logo.png`).
  One text, two homes; nothing to keep in step by hand.
- `docs/third-party.md` is a copy of the repo-root THIRD-PARTY.md.
- `docs/releases.md` is every GitHub release, newest first, with its notes.
  In CI it comes from the GitHub API (`gh`); offline it falls back to the
  version tags in the local repository, so a local preview still has a
  page there.

Both are ignored by git; the site is rebuilt by .github/workflows/docs.yml
on every push that touches the docs, and on every published release.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
REPO = "aareman/KosherOS"


def index_from_readme(readme: str) -> str:
    text = readme
    text = text.replace("](docs/", "](")
    text = text.replace('src="docs/', 'src="')
    text = text.replace('src="branding/logo.png"', 'src="images/logo.png"')
    text = text.replace("](branding/logo.png)", "](images/logo.png)")
    text = text.replace("](THIRD-PARTY.md)", "](third-party.md)")
    # The readme's "Read the docs" link points at this very site now.
    text = text.replace("[Read the docs](architecture.md)", "[Read the docs](architecture.md)")
    return text


def releases_from_github() -> list[dict] | None:
    try:
        out = subprocess.run(
            ["gh", "release", "list", "--repo", REPO, "--limit", "200",
             "--json", "tagName,name,isPrerelease,publishedAt,isLatest"],
            check=True, capture_output=True, text=True).stdout
    except (OSError, subprocess.CalledProcessError):
        return None
    found = json.loads(out)
    for release in found:
        body = subprocess.run(
            ["gh", "release", "view", release["tagName"], "--repo", REPO,
             "--json", "body", "--jq", ".body"],
            check=True, capture_output=True, text=True).stdout
        release["body"] = body.strip()
    return found


def releases_from_git() -> list[dict]:
    """Offline stand-in: every v* tag, with the commit subjects since the tag before."""
    try:
        tags = subprocess.run(["git", "tag", "--list", "v*", "--sort=-creatordate"],
                              check=True, capture_output=True, text=True, cwd=ROOT
                              ).stdout.split()
    except (OSError, subprocess.CalledProcessError):
        return []
    found = []
    for i, tag in enumerate(tags):
        prev = tags[i + 1] if i + 1 < len(tags) else None
        span = f"{prev}..{tag}" if prev else tag
        log = subprocess.run(["git", "log", "--format=- %s", span], check=True,
                             capture_output=True, text=True, cwd=ROOT).stdout.strip()
        found.append({"tagName": tag, "name": f"KosherOS {tag[1:]}",
                      "isPrerelease": "-pre." in tag, "publishedAt": "", "isLatest": False,
                      "body": log or "(no commits recorded locally)"})
    return found


def releases_page(found: list[dict]) -> str:
    lines = ["# Releases", "",
             "Every push to the repository becomes a pre-release on the **edge** channel; "
             "a person promotes one that has run well to **stable**. What each build changed "
             "is listed under it. The current stable is marked. "
             "See [deployment](deployment.md) for how machines move between versions.", ""]
    if not found:
        lines += ["_No releases yet._"]
    for release in found:
        badge = " · **stable**" if release.get("isLatest") else (
            " · pre-release" if release.get("isPrerelease") else "")
        when = (release.get("publishedAt") or "")[:10]
        lines.append(f"## {release['name']}{badge}")
        lines.append("")
        meta = f"`{release['tagName']}`" + (f" · {when}" if when else "")
        lines.append(meta)
        lines.append("")
        lines.append(release.get("body") or "")
        lines.append("")
    return "\n".join(lines)


def generate() -> None:
    (DOCS / "images").mkdir(exist_ok=True)
    shutil.copy(ROOT / "branding/logo.png", DOCS / "images/logo.png")
    (DOCS / "index.md").write_text(index_from_readme((ROOT / "readme.md").read_text()))
    # The third-party notices live at the repo root, where licences are
    # looked for; the site gets a copy.
    (DOCS / "third-party.md").write_text((ROOT / "THIRD-PARTY.md").read_text())
    found = releases_from_github()
    source = "GitHub"
    if found is None:
        found = releases_from_git()
        source = "local git tags"
    (DOCS / "releases.md").write_text(releases_page(found))
    print(f"docs/index.md and docs/releases.md written ({len(found)} releases, from {source})")


def main(argv=None) -> int:
    args = sys.argv[1:] if argv is None else argv
    generate()
    if "--build" in args:
        return subprocess.run(["mkdocs", "build", "--strict"], cwd=ROOT).returncode
    if "--serve" in args:
        return subprocess.run(["mkdocs", "serve"], cwd=ROOT).returncode
    return 0


if __name__ == "__main__":
    sys.exit(main())
