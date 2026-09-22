#!/usr/bin/env python3
"""The product version, read from the tags.

A commit's version is not written anywhere; it is worked out from the
history, the same way by everyone who asks:

    the last `v*` tag reachable from the commit      is where the count starts
    what landed since it, by conventional subject    says how far to step:
        a `feat` anywhere                            the minor moves, the patch resets
        anything else                                the patch moves
        a breaking change                            the minor moves, which is what 0.x means

A commit that IS tagged is that version. A commit that is not is the version
it would be released as — `0.7.0` on master, where the release step tags it
with exactly that number once the build has passed — or, for a build made
anywhere else, the same number marked as not a release: `0.7.0-dev.3+g2049571`
(three commits past the last tag, at that commit).

The number used to live in a `VERSION` file that CI committed to master
before building. That spent the number before anything downstream had run,
and a release step that failed left a hole (0.3.0 was one, and then 0.4.2,
0.5.0 and 0.6.0 behind it). Now the tag is the version, nothing is committed,
and the number is spent only when the tag lands — last, after the image is
built and pushed. The build passes the number to the Containerfile
(`--build-arg KOSHER_VERSION=$(scripts/version.py show)`), which writes it
into the image for os-release, the installer and the daemon to read.

**The major never moves on its own.** 1.0.0 is a decision about the product,
not the result of arithmetic, and the user asked for it to stay out of the
machine's hands: a major version is a person pushing the tag `v1.0.0`.
"""

from __future__ import annotations

import re
import subprocess
import sys
from collections.abc import Iterable
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Version tags only. A moving `stable` or `edge` tag would otherwise be the
# nearest tag and break the count.
TAG_GLOB = "v[0-9]*"

RELEASE = re.compile(r"^(\d+)\.(\d+)(?:\.(\d+))?$")
# The old series, still readable because those tags are in the history.
# Nothing writes this form any more.
LEGACY_PRE = re.compile(r"^(\d+)\.(\d+)\.(\d+)-pre\.(\d+)$")

PATCH, MINOR, MAJOR = "patch", "minor", "major"

# What a change has to say about itself. The subject's type decides; a "!"
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
    raise ValueError(f"version {text!r} is not MAJOR.MINOR.PATCH")


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
    """The version that follows `text` for a change of this kind."""
    major, minor, patch, pre = parse(text)
    if pre is not None:
        # Stepping off the old 0.1.0-pre.N series: dropping the suffix is
        # the release that series was leading to. One-time, by definition.
        return fmt(major, minor, patch)
    if kind == MAJOR:
        raise ValueError(
            "a major version is a decision, not an increment — push the tag "
            "by hand when the product is ready for it")
    if kind == MINOR:
        return fmt(major, minor + 1, 0)
    if kind != PATCH:
        raise ValueError(f"unknown kind of change {kind!r}")
    return fmt(major, minor, patch + 1)


def _git(*args: str, cwd: Path = ROOT) -> str:
    return subprocess.run(["git", *args], check=True, cwd=cwd,
                          capture_output=True, text=True).stdout.strip()


def exact_tag(commit: str = "HEAD", cwd: Path = ROOT) -> str | None:
    """The version tag ON this commit, if it has one."""
    try:
        return _git("describe", "--tags", "--exact-match", "--match", TAG_GLOB,
                    commit, cwd=cwd) or None
    except (subprocess.CalledProcessError, OSError):
        return None


def last_tag(commit: str = "HEAD", cwd: Path = ROOT) -> str | None:
    """The nearest version tag reachable from this commit, itself included.

    By the history rather than by date: two tags made in the same second
    sort arbitrarily, and the answer decides whether a feature already
    numbered gets counted a second time.
    """
    try:
        return _git("describe", "--tags", "--abbrev=0", "--match", TAG_GLOB,
                    commit, cwd=cwd) or None
    except (subprocess.CalledProcessError, OSError):
        return None


def messages_since(tag: str | None, commit: str = "HEAD", cwd: Path = ROOT) -> list[str]:
    """The commit messages between the tag and the commit, newest first.

    With no tag to go by, only the commit in hand is read, which is the
    conservative answer.
    """
    span = f"{tag}..{commit}" if tag else f"{commit}~1..{commit}"
    try:
        log = _git("log", "--format=%B%x1f", span, cwd=cwd)
    except (subprocess.CalledProcessError, OSError):
        return []
    return [part for part in log.split("\x1f") if part.strip()]


def kind_since(tag: str | None, commit: str = "HEAD", cwd: Path = ROOT) -> str:
    return kind_of(messages_since(tag, commit, cwd))


def release_version(commit: str = "HEAD", cwd: Path = ROOT) -> str:
    """The version this commit is, or would be released as.

    Raises when no version tag is reachable at all: the answer would be a
    guess, and a shallow clone (`fetch-depth: 1`) is the usual reason.
    """
    if tag := exact_tag(commit, cwd):
        return tag[1:]
    tag = last_tag(commit, cwd)
    if tag is None:
        raise ValueError(f"no version tag is reachable from {commit}; "
                         "fetch the tags (git fetch --tags) and try again")
    return next_version(tag[1:], kind_since(tag, commit, cwd))


def dev_version(commit: str = "HEAD", cwd: Path = ROOT) -> str:
    """What a build made anywhere but the release step calls itself.

    The number it would be released as, marked as not a release, with the
    distance from the last tag and the commit — so two local builds are
    told apart and neither is mistaken for a release. Without a tag or a
    repository to read (a shallow clone, an unpacked tarball) it is
    `0.0.0-dev...`, which is at least honest.
    """
    if tag := exact_tag(commit, cwd):
        return tag[1:]
    try:
        sha = _git("rev-parse", "--short=7", commit, cwd=cwd)
    except (subprocess.CalledProcessError, OSError):
        return "0.0.0-dev.0"
    tag = last_tag(commit, cwd)
    if tag is None:
        count = _git("rev-list", "--count", commit, cwd=cwd)
        return f"0.0.0-dev.{count}+g{sha}"
    count = _git("rev-list", "--count", f"{tag}..{commit}", cwd=cwd)
    return f"{release_version(commit, cwd)}-dev.{count}+g{sha}"


USAGE = """usage: version.py [show|release|kind|tag]

  show     the version of this checkout: the tag on it, or the number it
           would be released as marked -dev (default)
  release  the version this commit is or would be released as; fails when
           no version tag can be reached
  kind     the step the changes since the last tag ask for: minor or patch
  tag      the release version as its tag, v0.7.0"""


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    command = args[0] if args else "show"
    try:
        if command == "show":
            print(dev_version(cwd=ROOT))
        elif command == "release":
            print(release_version(cwd=ROOT))
        elif command == "kind":
            print(kind_since(last_tag(cwd=ROOT), cwd=ROOT))
        elif command == "tag":
            print("v" + release_version(cwd=ROOT))
        else:
            print(USAGE, file=sys.stderr)
            return 2
    except ValueError as error:
        print(f"version.py: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
