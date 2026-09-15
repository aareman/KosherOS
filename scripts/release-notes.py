#!/usr/bin/env python3
"""Write a release's notes from the commits it contains.

    scripts/release-notes.py --version 0.1.0-pre.24 --previous v0.1.0-pre.23 \
        --sha $GITHUB_SHA --image ghcr.io/aareman/kosher-linux --built yes

The notes a person actually reads. One sentence first saying what this build
means for them: how many new things and fixes, or that nothing on their
machine changes. Then how to get it, then the changes, filed as new, fixed or
changed and labelled with the part of the product each touched.

Structure has to be earned. A build with one change gets one line and no
headings at all; headings appear only once there are enough lines to sort.
Work on the docs, the CI or the build scripts is never listed — a family
does not install a release for a docs edit — it is counted in one trailing
sentence so the history is still honest.

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
    (r"\bwhitelist|approved[- ]site|\bcategor", "Filter"),
    (r"\bupdate|\brollback|\bchannel|\brelease", "Updates"),
)

FIX_WORDS = re.compile(
    r"\b(fix(?:e[sd])?|bug|broke|broken|break(?:s|ing)?|fail(?:s|ed|ing|ure)?|crash|"
    r"hang|freez|regress|stray|wrong|mistake|race|leak|repair|restore[sd]?|"
    r"no longer|instead of|silently|misread|refus)", re.I)
FEATURE_WORDS = re.compile(
    r"\b(add(?:s|ed)?|new|introduc|now (?:has|shows|carries|opens)|ready-made|"
    r"support(?:s|ed)? for|can now|gets? (?:a|an|its)|per[- ]account)", re.I)

SECTIONS = (("feat", "## ✨ New"), ("fix", "## 🔧 Fixed"), ("change", "## 🔁 Changed"))
MARKERS = {"feat": "✨", "fix": "🔧", "change": "🔁"}
MAX_PER_SECTION = 8
NUMBERS = ("no", "one", "two", "three", "four", "five", "six", "seven", "eight",
           "nine", "ten")


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


def area_counts(files: list[str]) -> dict[str, int]:
    """How many of a commit's non-test files fall in each part of the product."""
    real = [f for f in files if "test" not in f] or files
    counts: dict[str, int] = {}
    for path in real:
        for prefix, label in AREAS:
            if path.startswith(prefix) or prefix in path:
                counts[label] = counts.get(label, 0) + 1
                break
    return counts


def area(files: list[str]) -> str:
    """The part of the product a commit is mostly about.

    Counted across the files it touched, tests set aside: a change with
    twelve files under admin-app/ and one test elsewhere is an Admin
    change. First match per file, in the order AREAS lists.
    """
    counts = area_counts(files)
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
    if quiet:
        return "quiet"  # docs, CI and build work, however the subject reads
    conventional = re.match(r"^(\w+)(\([^)]*\))?!?:", text)
    if conventional:
        kind = conventional.group(1).lower()
        if kind in ("feat", "feature"):
            return "feat"
        if kind in ("fix", "bugfix", "hotfix", "perf"):
            return "fix"
        if kind in ("docs", "ci", "build", "chore", "test", "refactor", "style"):
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


def bullets(found: list[tuple[str, list[str]]]) -> tuple[dict[str, list[str]], list[str]]:
    """The lines worth a reader's time, grouped, plus the areas of the rest.

    Returns (grouped, quiet): grouped has a list per user-visible kind;
    quiet is one area name per housekeeping line, so the caller can say
    "three changes to the docs and CI" without listing them.
    """
    grouped: dict[str, list[str]] = {key: [] for key, _heading in SECTIONS}
    quiet: list[str] = []
    seen: set[str] = set()
    for subject, files in found:
        commit_area = area(files)
        # Tests alone change nothing a family can see, however the subject reads.
        tests_only = bool(files) and all("test" in f for f in files)
        # A "feat:" or "fix:" prefix on the subject speaks for every line in it.
        prefix = re.match(r"^\w+(\([^)]*\))?!?:", subject)
        # The files settle the label when they all sit in one part; the words
        # only decide when a commit spans several, or when there are none.
        spread = len(area_counts(files)) != 1
        for point in points(subject):
            label = area_for(point, commit_area) if spread else commit_area
            said = f"{prefix.group(0)} {point}" if prefix else point
            kind = classify(said, quiet=(tests_only or commit_area in QUIET_AREAS
                                         or label in QUIET_AREAS))
            line = f"**{label}** — {point}" if label else point
            if line.lower() in seen:
                continue
            seen.add(line.lower())
            if kind == "quiet":
                quiet.append("Tests" if tests_only else commit_area or label or "Build")
            else:
                grouped[kind].append(line)
    return grouped, quiet


def count(n: int, one: str, many: str) -> str:
    word = NUMBERS[n] if n < len(NUMBERS) else str(n)
    return f"{word} {one if n == 1 else many}"


def join(parts: list[str]) -> str:
    if len(parts) <= 1:
        return "".join(parts)
    return ", ".join(parts[:-1]) + " and " + parts[-1]


def headline(grouped: dict[str, list[str]]) -> str:
    """'Two new things and one fix.' — what the build holds, in words."""
    parts = []
    if grouped["feat"]:
        parts.append(count(len(grouped["feat"]), "new thing", "new things"))
    if grouped["fix"]:
        parts.append(count(len(grouped["fix"]), "fix", "fixes"))
    if grouped["change"]:
        parts.append(count(len(grouped["change"]), "change", "changes"))
    text = join(parts)
    return text[0].upper() + text[1:] + "."


QUIET_NAMES = {"Docs": "the docs", "CI": "CI", "Build": "the build tooling",
               "Tests": "the tests", "Installer": "the installer"}


def housekeeping(quiet: list[str]) -> str:
    """'Behind the scenes: three changes to the docs and CI.'"""
    named = join(sorted({QUIET_NAMES.get(a, a.lower()) for a in quiet}))
    return f"Behind the scenes: {count(len(quiet), 'change', 'changes')} to {named}."


def notes(version: str, previous: str, sha: str, image: str, built: str,
          image_failed: bool = False) -> str:
    tag = f"v{version}"
    grouped, quiet = bullets(commits(previous, sha))
    total = sum(len(v) for v in grouped.values())
    lines: list[str] = []

    # First: what this build means for the reader.
    if total:
        lines.append(f"**{headline(grouped)}**")
    elif quiet:
        lines.append("**Nothing on a KosherOS machine changes in this build.**")
    else:
        lines.append("**Nothing but the version number changed.**")
    lines.append("")

    # Then how to get it, or why there is nothing to get. One line each.
    if built == "yes":
        lines += ["```sh", f"bootc switch {image}:{tag}", "```",
                  "Signed with cosign. Machines on **edge** pick this up on their next "
                  "update; **stable** machines stay where they are until someone "
                  "promotes it.", ""]
    elif image_failed:
        lines += ["⚠️ The image build failed, so no machine can move to this version. "
                  "The next build that succeeds carries these changes.", ""]
    elif total:
        lines += ["No new image: none of this reaches the OS itself, so machines stay "
                  "on the one they have.", ""]

    # Then the changes: a lone line stands alone, a list gets headings.
    if total == 1:
        kind, item = next((k, v[0]) for k, v in grouped.items() if v)
        lines += [f"{MARKERS[kind]} {item}", ""]
    elif total:
        for key, heading in SECTIONS:
            items = grouped[key]
            if not items:
                continue
            lines += [heading, ""]
            lines += [f"- {item}" for item in items[:MAX_PER_SECTION]]
            if len(items) > MAX_PER_SECTION:
                lines.append(f"- …and {len(items) - MAX_PER_SECTION} more")
            lines.append("")

    # Last: the rest, counted, and where to read more.
    footer = []
    if quiet:
        footer.append(housekeeping(quiet))
    if previous:
        footer.append(f"Since **{previous}**.")
    footer.append("[What works](https://aareman.github.io/KosherOS/supported/) · "
                  "[All releases](https://aareman.github.io/KosherOS/releases/)")
    lines.append(" ".join(footer))
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
