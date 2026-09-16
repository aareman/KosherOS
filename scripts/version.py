#!/usr/bin/env python3
"""The product version, and the semver step taken on every merge.

`VERSION` at the repo root is the one place the number lives: the
Containerfile copies it into os-release, the ISO is named after it, the
installer's welcome screen reads it from `.buildstamp`, the admin app's
Updates page shows it. It reads `MAJOR.MINOR.PATCH` — plain semver, with no
pre-release suffix. (It used to read `0.1.0-pre.N`, a release that never
arrived and a counter that reached seventy-one; every build was flagged as a
GitHub pre-release, so the releases page had no releases on it.)

`bump` is run by CI once for every push to master — every merge — and writes
the new number, commits VERSION to master and builds that commit, so every
image, ISO and release carries a different version and a bug report can say
which build it came from.

How far the number moves is taken from what landed, through the
conventional-commit subjects this repository already writes:

    a `feat` anywhere in the merge   the minor moves, the patch resets to 0
    anything else                    the patch moves
    a breaking change                the minor moves, which is what 0.x means

**The major never moves on its own.** 1.0.0 is a decision about the product,
not the result of arithmetic, and the user asked for it to stay out of the
machine's hands: a major version is a person editing VERSION.

Local builds between merges share the last merged version; the image's sha
tag tells them apart.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from collections.abc import Iterable
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERSION_FILE = ROOT / "VERSION"

RELEASE = re.compile(r"^(\d+)\.(\d+)(?:\.(\d+))?$")
# The old series, still readable so the first bump after the change can step
# off it cleanly. Nothing writes this form any more.
LEGACY_PRE = re.compile(r"^(\d+)\.(\d+)\.(\d+)-pre\.(\d+)$")

PATCH, MINOR, MAJOR = "patch", "minor", "major"

# What a merge has to say about itself. The subject's type decides; a "!"
# after the type, or BREAKING CHANGE in the body, is a break.
FEATURE = re.compile(r"^feat(\([^)]*\))?!?:", re.I)
BREAKING_TYPE = re.compile(r"^\w+(\([^)]*\))?!:")
BREAKING_BODY = re.compile(r"\bBREAKING[ -]CHANGE\b")


def parse(text: str) -> tuple[int, int, int, int | None]:
    """(major, minor, patch, legacy pre-release counter or None)."""
    text = text.strip()
    if m := LEGACY_PRE.fullmatch(text):
        major, minor, patch, pre = (int(g) for g in m.groups())
        return major, minor, patch, pre
    if m := RELEASE.fullmatch(text):
        major, minor, patch = m.group(1), m.group(2), m.group(3) or "0"
        return int(major), int(minor), int(patch), None
    raise ValueError(f"VERSION {text!r} is not MAJOR.MINOR.PATCH")


def fmt(major: int, minor: int, patch: int) -> str:
    return f"{major}.{minor}.{patch}"


def kind_of(messages: Iterable[str]) -> str:
    """The step a set of commit messages asks for: minor or patch.

    Never major. A breaking change is a minor step while the product is
    0.x — which is exactly what a zero major version is for.
    """
    for message in messages:
        message = (message or "").strip()
        if not message:
            continue
        subject = message.splitlines()[0].strip()
        if FEATURE.match(subject) or BREAKING_TYPE.match(subject) \
                or BREAKING_BODY.search(message):
            return MINOR
    return PATCH


def next_version(text: str, kind: str = PATCH) -> str:
    """The version a merge of this kind carries."""
    major, minor, patch, pre = parse(text)
    if pre is not None:
        # Stepping off the old 0.1.0-pre.N series: dropping the suffix is
        # the release that series was leading to. One-time, by definition.
        return fmt(major, minor, patch)
    if kind == MAJOR:
        raise ValueError(
            "a major version is a decision, not an increment — edit VERSION "
            "by hand when the product is ready for it")
    if kind == MINOR:
        return fmt(major, minor + 1, 0)
    if kind != PATCH:
        raise ValueError(f"unknown kind of change {kind!r}")
    return fmt(major, minor, patch + 1)


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(["git", *args], check=True, cwd=cwd,
                          capture_output=True, text=True).stdout.strip()


def last_version_tag(cwd: Path) -> str | None:
    """The last version tag on this history, which is where the last number
    was cut.

    By the history rather than by date: two tags made in the same second
    sort arbitrarily, and the answer decides whether a feature already
    numbered gets counted a second time.
    """
    try:
        return _git("describe", "--tags", "--abbrev=0", "--match", "v*", cwd=cwd) or None
    except (subprocess.CalledProcessError, OSError):
        return None


def merge_messages(cwd: Path) -> list[str]:
    """The commit messages this merge brings, newest first.

    Since the last version tag, because that is where the last number was
    cut. With no tag to go by — a fresh clone with no tags fetched — only
    the commit in hand is read, which is the conservative answer.
    """
    tag = last_version_tag(cwd)
    span = f"{tag}..HEAD" if tag else "HEAD~1..HEAD"
    try:
        log = _git("log", "--format=%B%x1f", span, cwd=cwd)
    except (subprocess.CalledProcessError, OSError):
        return []
    return [part for part in log.split("\x1f") if part.strip()]


def merge_kind(cwd: Path) -> str:
    return kind_of(merge_messages(cwd))


def bump(path: Path = VERSION_FILE, *, stage: bool = True,
         kind: str | None = None) -> str | None:
    """Write the next version and stage VERSION. Returns the new version, or
    None when this is a commit that must not be numbered."""
    git_dir = _git_dir(path.parent)
    if git_dir is not None and (git_dir / "MERGE_HEAD").exists():
        return None  # a merge carries no change of its own
    if os.environ.get("KOSHER_NO_BUMP"):
        return None
    if kind is None:
        kind = merge_kind(path.parent)
    new = next_version(path.read_text(), kind)
    path.write_text(new + "\n")
    if stage:
        subprocess.run(["git", "add", "--", str(path)], check=True, cwd=path.parent)
    return new


def _git_dir(cwd: Path) -> Path | None:
    try:
        out = subprocess.run(["git", "rev-parse", "--git-dir"], check=True, cwd=cwd,
                             capture_output=True, text=True).stdout.strip()
    except (subprocess.CalledProcessError, OSError):
        return None
    return (cwd / out).resolve()


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    command = args[0] if args else "show"
    if command == "show":
        print(VERSION_FILE.read_text().strip())
    elif command == "next":
        print(next_version(VERSION_FILE.read_text(), merge_kind(VERSION_FILE.parent)))
    elif command == "kind":
        print(merge_kind(VERSION_FILE.parent))
    elif command == "bump":
        new = bump()
        if new is not None:
            print(f"version: {new}")
    else:
        print(__doc__.strip().splitlines()[0], file=sys.stderr)
        print("usage: version.py [show|next|kind|bump]", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
