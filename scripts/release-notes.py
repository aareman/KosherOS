#!/usr/bin/env python3
"""Write a release's notes from the commits it contains.

    scripts/release-notes.py --version 0.1.0-pre.24 --previous v0.1.0-pre.23 \
        --sha $GITHUB_SHA --image ghcr.io/aareman/kosher-linux --built yes

The notes a person actually reads: a line saying what this build is and how
to get it, then what changed, grouped into new things, fixes and the rest,
each labelled with the part of the product it touched. Not a wall of
sentences — an eye should find "Store" or "Admin" in a second and stop.

Two things make that possible without anybody writing changelog entries by
hand. The part of the product comes from the FILES a commit touched, which
is reliable in a way that reading the subject is not. And a subject written
as "A; B; C" — this repository's habit — becomes three bullets, because
three short lines are read and one long line is skipped.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Which part of the product a path belongs to, first match winning. The
# label is what appears in bold at the start of a bullet.
AREAS = (
    ("admin-app/", "Admin"),
    ("store-app/", "Store"),
    ("setup-app/", "Setup"),
    ("myfilter-app/", "My Filter"),
    ("search-app/", "Search"),
    ("mitm/", "Filtering"),
    ("kosherd/src/kosherd/vision", "Pictures"),
    ("kosherd/src/kosherd/imageedit", "Pictures"),
    ("kosherd/src/kosherd/videocheck", "Pictures"),
    ("kosherd/", "Filter"),
    ("portal/", "Portal"),
    ("os-image/", "System"),
    ("branding/", "Branding"),
    ("policy/", "Policy"),
    ("docs/", "Docs"),
    ("readme.md", "Docs"),
    ("mkdocs.yml", "Docs"),
    (".github/", "CI"),
    ("scripts/", "Build"),
    ("Justfile", "Build"),
    ("devenv", "Build"),
)
# Areas whose changes are housekeeping however they are worded: nobody
# installs a KosherOS release for a docs edit or a CI fix.
QUIET_AREAS = {"Docs", "CI", "Build"}
# What a bullet says about itself, which beats guessing from files when a
# commit touches several parts at once.
BY_WORD = (
    (r"\bstore\b", "Store"),
    (r"\badmin(?: app)?\b|\bfamily board\b|\bguest\b|\bpreset", "Admin"),
    (r"\bwizard\b|first[- ]boot", "Setup"),
    (r"\bmy filter\b", "My Filter"),
    (r"\bsearch\b", "Search"),
    (r"\binstaller\b|\bISO\b|anaconda", "Installer"),
    (r"\bGRUB\b|boot splash|plymouth|boot ?loop", "Boot"),
    (r"\bpicture|\bimage filter|\bvideo\b", "Pictures"),
    (r"\bwhitelist|approved[- ]site|\bcategor", "Filtering"),
    (r"\bupdate|\brollback|\bchannel|\brelease", "Updates"),
)

FIX_WORDS = re.compile(
    r"\b(fix(?:e[sd])?|bug|broke|broken|break(?:s|ing)?|fail(?:s|ed|ing|ure)?|crash|"
    r"hang|freez|regress|stray|wrong|mistake|race|leak|repair|restore[sd]?|"
    r"no longer|instead of|silently|misread|refus)", re.I)
FEATURE_WORDS = re.compile(
    r"\b(add(?:s|ed)?|new|introduc|now (?:has|shows|carries|opens)|ready-made|"
    r"support(?:s|ed)? for|can now|gets? (?:a|an|its)|per[- ]account)", re.I)

SECTIONS = (("feat", "## ✨ New"), ("fix", "## 🔧 Fixed"),
            ("change", "## 🔁 Changed"), ("quiet", "## 🧹 Behind the scenes"))
MAX_PER_SECTION = 10


def run(*args: str) -> str:
    return subprocess.run(["git", *args], check=True, capture_output=True,
                          text=True, cwd=ROOT).stdout.strip()


def commits(previous: str, sha: str) -> list[tuple[str, list[str]]]:
    """(subject, files) for each commit in the range, newest first."""
    span = f"{previous}..{sha}" if previous else f"{sha}~8..{sha}"
    try:
        log = run("log", "--format=%H\t%s", span)
    except subprocess.CalledProcessError:
        return []
    found = []
    for line in log.splitlines():
        commit, _, subject = line.partition("\t")
        if not subject or subject.lower().startswith(("merge ", "bump ")):
            continue
        try:
            files = run("show", "--name-only", "--format=", commit).splitlines()
        except subprocess.CalledProcessError:
            files = []
        found.append((subject, [f for f in files if f and f != "VERSION"]))
    return found


def area(files: list[str]) -> str:
    """The part of the product a commit is mostly about.

    Counted across the files it touched, tests set aside: a change with
    twelve files under admin-app/ and one test elsewhere is an Admin
    change. First match per file, in the order AREAS lists.
    """
    real = [f for f in files if "test" not in f] or files
    counts: dict[str, int] = {}
    for path in real:
        for prefix, label in AREAS:
            if path.startswith(prefix) or prefix in path:
                counts[label] = counts.get(label, 0) + 1
                break
    if not counts:
        return ""
    return max(counts, key=lambda label: (counts[label], -list(
        dict(AREAS).values()).index(label) if label in dict(AREAS).values() else 0))


def area_for(point: str, fallback: str) -> str:
    """What this particular line is about, or the commit's own area."""
    for pattern, label in BY_WORD:
        if re.search(pattern, point, re.I):
            return label
    return fallback


def classify(text: str, quiet: bool = False) -> str:
    """new, fixed, changed, or housekeeping — for one line, not a whole commit.

    Per line, because a commit that says "ready-made lists; a whitelist
    account is no longer asked about categories" is one of each, and
    filing the pair under either heading is a small lie.
    """
    conventional = re.match(r"^(\w+)(\([^)]*\))?!?:", text)
    if conventional:
        kind = conventional.group(1).lower()
        if kind in ("feat", "feature"):
            return "feat"
        if kind in ("fix", "bugfix", "hotfix", "perf"):
            return "fix"
        if kind in ("docs", "ci", "build", "chore", "test", "refactor", "style"):
            return "quiet"
    if quiet:
        return "quiet"
    if FIX_WORDS.search(text):
        return "fix"
    if FEATURE_WORDS.search(text):
        return "feat"
    return "change"


def points(subject: str) -> list[str]:
    """One subject, split into the things it actually says.

    "A; B; C" becomes three bullets: three short lines get read and one
    long line gets skipped.
    """
    text = re.sub(r"^\w+(\([^)]*\))?!?:\s*", "", subject).strip()
    parts = [p.strip() for p in re.split(r";\s+", text) if p.strip()]
    out = []
    for part in parts:
        part = part[0].upper() + part[1:] if part else part
        out.append(part.rstrip("."))
    return out or [text]


def bullets(found: list[tuple[str, list[str]]]) -> dict[str, list[str]]:
    grouped: dict[str, list[str]] = {key: [] for key, _heading in SECTIONS}
    seen: set[str] = set()
    for subject, files in found:
        commit_area = area(files)
        quiet = commit_area in QUIET_AREAS
        for point in points(subject):
            label = area_for(point, commit_area)
            kind = classify(point, quiet=quiet or label in QUIET_AREAS)
            line = f"**{label}** — {point}" if label else point
            if line.lower() in seen:
                continue
            seen.add(line.lower())
            grouped[kind].append(line)
    return grouped


def notes(version: str, previous: str, sha: str, image: str, built: str,
          image_failed: bool = False) -> str:
    tag = f"v{version}"
    lines: list[str] = []
    if built == "yes":
        lines += [
            f"### 📦 `{image}:{tag}` · edge channel",
            "",
            "```sh",
            f"bootc switch {image}:{tag}",
            "```",
            "Signed with cosign. Machines on **edge** pick this up on their next "
            "update; **stable** machines are unaffected until someone promotes it.",
        ]
    elif image_failed:
        lines += ["### ⚠️ No image for this version",
                  "",
                  "The code passed its tests, so the version and its changes are "
                  "recorded here, but the image build failed and no machine can "
                  "move to it. The next successful build carries these changes."]
    else:
        lines += ["### 📄 No new image",
                  "",
                  "Nothing in this version reaches the OS image, so machines stay "
                  "on the previous one."]
    lines.append("")

    grouped = bullets(commits(previous, sha))
    if not any(grouped.values()):
        lines += ["Nothing but the version number changed."]
    for key, heading in SECTIONS:
        items = grouped[key]
        if not items:
            continue
        lines += [heading, ""]
        for item in items[:MAX_PER_SECTION]:
            lines.append(f"- {item}")
        if len(items) > MAX_PER_SECTION:
            lines.append(f"- …and {len(items) - MAX_PER_SECTION} more")
        lines.append("")
    if previous:
        lines.append(f"Since **{previous}**.")
    lines.append("[What works](https://aareman.github.io/KosherOS/supported/) · "
                 "[All releases](https://aareman.github.io/KosherOS/releases/) · "
                 "[Docs](https://aareman.github.io/KosherOS/)")
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--version", required=True)
    p.add_argument("--previous", default="")
    p.add_argument("--sha", default="HEAD")
    p.add_argument("--image", default="ghcr.io/aareman/kosher-linux")
    p.add_argument("--built", choices=["yes", "no"], default="no")
    p.add_argument("--image-failed", action="store_true")
    p.add_argument("--out", type=Path)
    args = p.parse_args(argv)
    text = notes(args.version, args.previous, args.sha, args.image, args.built,
                 args.image_failed)
    if args.out:
        args.out.write_text(text)
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
