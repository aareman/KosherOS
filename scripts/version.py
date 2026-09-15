#!/usr/bin/env python3
"""The product version, and the pre-release counter that ticks on every commit.

`VERSION` at the repo root is the one place the number lives: the
Containerfile copies it into os-release, the ISO is named after it, the
installer's welcome screen reads it from `.buildstamp`, the admin app's
Updates page shows it. It reads `MAJOR.MINOR.PATCH-pre.N` between releases
and a plain `MAJOR.MINOR.PATCH` at a release.

`bump` runs as a git pre-commit hook (devenv's git-hooks, on prek): it
increments N and stages VERSION, so the bump lands in the commit being made
and every commit — every image, every ISO, every VM — carries a different
version. That is what lets a bug report say which build it came from. A
merge commit is left alone: it introduces no change of its own. A release
is a person editing VERSION to `X.Y.Z`; the next commit starts
`X.Y.(Z+1)-pre.1` by itself.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERSION_FILE = ROOT / "VERSION"

RELEASE = re.compile(r"^(\d+)\.(\d+)(?:\.(\d+))?$")
PRERELEASE = re.compile(r"^(\d+)\.(\d+)\.(\d+)-pre\.(\d+)$")


def parse(text: str) -> tuple[int, int, int, int | None]:
    """(major, minor, patch, pre) — pre is None for a release."""
    text = text.strip()
    if m := PRERELEASE.fullmatch(text):
        major, minor, patch, pre = (int(g) for g in m.groups())
        return major, minor, patch, pre
    if m := RELEASE.fullmatch(text):
        major, minor, patch = m.group(1), m.group(2), m.group(3) or "0"
        return int(major), int(minor), int(patch), None
    raise ValueError(f"VERSION {text!r} is neither X.Y.Z nor X.Y.Z-pre.N")


def fmt(major: int, minor: int, patch: int, pre: int | None) -> str:
    base = f"{major}.{minor}.{patch}"
    return base if pre is None else f"{base}-pre.{pre}"


def next_version(text: str) -> str:
    """The version the next commit carries."""
    major, minor, patch, pre = parse(text)
    if pre is None:
        # Just released X.Y.Z: development of the next patch begins.
        return fmt(major, minor, patch + 1, 1)
    return fmt(major, minor, patch, pre + 1)


def bump(path: Path = VERSION_FILE, *, stage: bool = True) -> str | None:
    """Increment the pre-release counter and stage VERSION. Returns the new
    version, or None when the commit is one that must not be bumped."""
    git_dir = _git_dir(path.parent)
    if git_dir is not None and (git_dir / "MERGE_HEAD").exists():
        return None  # a merge carries no change of its own
    if os.environ.get("KOSHER_NO_BUMP"):
        return None
    new = next_version(path.read_text())
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
        print(next_version(VERSION_FILE.read_text()))
    elif command == "bump":
        new = bump()
        if new is not None:
            print(f"version: {new}")
    else:
        print(__doc__.strip().splitlines()[0], file=sys.stderr)
        print("usage: version.py [show|next|bump]", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
