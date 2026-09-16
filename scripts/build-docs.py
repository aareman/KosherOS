#!/usr/bin/env python3
"""Assemble the docs site's generated pages, then (optionally) build it.

    scripts/build-docs.py            # write docs/index.md, docs/releases.md, copy the logo
    scripts/build-docs.py --build    # ...and run mkdocs build
    scripts/build-docs.py --serve    # ...and run mkdocs serve

Two pages are generated rather than written:

The front page is NOT generated: `docs/index.md` is written for the site.
It used to be the readme with its links rewritten, and that came out
broken — the readme is full of raw HTML (a centred div, badges, a details
block) and Python-Markdown does not render Markdown inside raw HTML, so
the front page showed its own source. The readme is for GitHub; the front
page is for the site.

- `docs/third-party.md` is a copy of the repo-root THIRD-PARTY.md.
- `docs/releases.md` is every GitHub release, newest first. The newest are
  listed in full — flattened, so the page has one heading per release and
  says the install and signing boilerplate once — and older ones are a
  table row each (see `digest`).
  In CI it comes from the GitHub API (`gh`); offline it falls back to the
  version tags in the local repository, so a local preview still has a
  page there.

Both are ignored by git; the site is rebuilt by the docs job in
.github/workflows/ci.yml, in the same run that publishes the release.
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


def newest_first(found: list[dict]) -> list[dict]:
    """Sort by when each was published, not by what GitHub hands back.

    GitHub orders releases by comparing the tag as TEXT, which put
    "pre.9" above "pre.13" in the old numbering and will put "0.9.0" above
    "0.10.0" in this one. Sorting by date sidesteps the question entirely.
    """
    return sorted(found, key=lambda r: (r.get("publishedAt") or "", r.get("tagName") or ""),
                  reverse=True)


DETAILED = 12  # releases shown in full; the rest are one table row each
GITHUB_RELEASES = f"https://github.com/{REPO}/releases"

# What the kinds inside a release's notes are called, and the marker each
# line gets instead of a heading. A heading inside a release would sit in
# the page's table of contents beside the releases themselves — forty
# releases became a hundred and sixty entries — so the kind rides on the
# line. An empty marker is a plain change.
KINDS = (("new", "✨"), ("fix", "🔧"), ("what changed", ""), ("changed", "🔁"),
         ("behind the scenes", "🧹"))
# Sections the older notes carried that say the same thing in every
# release: how the build is installed, what works on a filtered account.
DROPPED_SECTIONS = ("what this build is", "what is supported", "no new image")
# Lines every release repeats because its notes stand alone on GitHub.
# On a page of many releases they are said once, at the top.
NOISE = ("Signed with cosign", "Since **", "[What works]", "No new image:",
         "Nothing in this version reaches", "An installer ISO is built")


def digest(body: str) -> dict:
    """One release's notes reduced to what belongs on a page of many.

    Each release's notes are written to be read alone on GitHub, so they
    say how to install the build, that it is signed, where the other
    releases are, and they sort the changes under headings. Listed forty
    at a time that is the same four lines forty times and a table of
    contents nobody can use. This keeps the changes (each with a marker
    for its kind), the housekeeping count, a warning such as a failed
    image, and whether the build reached the OS image; it drops the rest.
    """
    out = {"headline": "", "bullets": [], "quiet": None, "notes": [], "no_image": False}
    marker, keep, in_code, quiet_lines = "", True, False, 0
    for raw in (body or "").splitlines():
        line = raw.strip()
        if line.startswith("```"):
            in_code = not in_code
            continue
        if in_code or not line or line.startswith(">"):
            continue  # the install command; the renaming note old releases carry
        heading = re.match(r"#+\s*(.*)", line)
        if heading:
            title = re.sub(r"[^\w ]", "", heading.group(1)).strip().lower()
            keep = not any(dropped in title for dropped in DROPPED_SECTIONS)
            out["no_image"] = out["no_image"] or "no new image" in title
            marker = next((m for name, m in KINDS if name in title), "")
            continue
        if "no new image" in line.lower() or "no new os image" in line.lower():
            out["no_image"] = True
        said = re.search(r"Behind the scenes: [^.]+\.", line)
        if said:
            out["quiet"] = said.group(0)
            continue
        if any(noise in line for noise in NOISE) or not keep:
            continue
        bullet = re.match(r"[-*]\s+(.*)", line)
        if bullet and marker == "🧹":
            quiet_lines += 1  # housekeeping is a count, not a list
            continue
        if bullet:
            out["bullets"].append((marker, bullet.group(1).strip()))
            continue
        lone = re.match(r"([✨🔧🔁])\s+(.*)", line)
        if lone:
            out["bullets"].append((lone.group(1), lone.group(2).strip()))
            continue
        if re.fullmatch(r"\*\*[^*]+\*\*", line) and not out["headline"] and not out["bullets"]:
            out["headline"] = line.strip("*")
            continue
        out["notes"].append(line)
    if quiet_lines and not out["quiet"]:
        out["quiet"] = f"Behind the scenes: {quiet_lines} change{'s' if quiet_lines != 1 else ''}."
    return out


def version_of(release: dict) -> str:
    """'0.1.0-pre.057' — the heading, short enough for a table of contents."""
    name = release.get("name") or ""
    if name.startswith("KosherOS "):
        return name[len("KosherOS "):]
    tag = release.get("tagName") or name
    return tag[1:] if tag.startswith("v") else tag


def _cell(text: str) -> str:
    return text.replace("|", "\\|")


def releases_page(found: list[dict]) -> str:
    found = newest_first(found)
    lines = ["# Releases", "",
             "Every merge becomes a release on the **edge** channel; a person "
             "promotes one that has run well to **stable**. Machines on edge take each "
             "build at their next update; stable machines move when a build is promoted. "
             "Any build can be pinned with `bootc switch ghcr.io/aareman/kosher-linux:vVERSION`; "
             "every image is signed with cosign. See [deployment](deployment.md) for how "
             "machines move between versions.", "",
             f"The newest builds are listed in full and older ones one line each; the "
             f"complete notes for every build are on [GitHub]({GITHUB_RELEASES}).", ""]
    if not found:
        lines += ["_No releases yet._"]
    for release in found[:DETAILED]:
        parts = digest(release.get("body") or "")
        when = (release.get("publishedAt") or "")[:10]
        meta = [f"`{release['tagName']}`", when,
                "**stable**" if release.get("isLatest") else "",
                "no new image" if parts["no_image"] else ""]
        lines += [f"## {version_of(release)}", "", " · ".join(m for m in meta if m), ""]
        if parts["bullets"]:
            lines += [f"- {marker} {text}".replace("-  ", "- ", 1)
                      for marker, text in parts["bullets"]]
            lines.append("")
        elif parts["headline"]:
            lines += [parts["headline"], ""]
        for note in parts["notes"]:
            lines += [note, ""]
        if parts["quiet"]:
            lines += [f"*{parts['quiet']}*", ""]
    older = found[DETAILED:]
    if older:
        lines += ["## Earlier releases", "", "| Version | Date | What changed |",
                  "|---|---|---|"]
        for release in older:
            parts = digest(release.get("body") or "")
            when = (release.get("publishedAt") or "")[:10]
            if parts["bullets"]:
                marker, text = parts["bullets"][0]
                summary = f"{marker} {text}".strip()
                if len(parts["bullets"]) > 1:
                    summary += f" · and {len(parts['bullets']) - 1} more"
            elif parts["headline"]:
                summary = parts["headline"]
            elif parts["quiet"]:
                summary = "Behind the scenes only"
            else:
                summary = "—"
            version = f"`{release['tagName']}`"
            if release.get("isLatest"):
                version += " **stable**"
            lines.append(f"| {version} | {when} | {_cell(summary)} |")
        lines.append("")
    return "\n".join(lines)


def generate() -> None:
    (DOCS / "images").mkdir(exist_ok=True)
    shutil.copy(ROOT / "branding/logo.png", DOCS / "images/logo.png")
    # The third-party notices live at the repo root, where licences are
    # looked for; the site gets a copy.
    (DOCS / "third-party.md").write_text((ROOT / "THIRD-PARTY.md").read_text())
    found = releases_from_github()
    source = "GitHub"
    if found is None:
        found = releases_from_git()
        source = "local git tags"
    (DOCS / "releases.md").write_text(releases_page(found))
    print(f"docs/releases.md and docs/third-party.md written "
          f"({len(found)} releases, from {source})")


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
