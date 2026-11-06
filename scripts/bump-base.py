#!/usr/bin/env python3
"""Build FROM our own copy of Fedora's base images, pinned by digest.

The image is built FROM Fedora's fedora-bootc:44 (and its builder stages
FROM fedora:44). Those tags move about once a day, and every move changes
the cache key of every layer after it: the next build started from
nothing, twenty-two minutes of it, to change a line of Python. Pinning
Fedora's own digests fixed that for a day (#243) and then broke every
build: quay.io deletes a digest once nothing points at it, and Fedora
publishes no dated tags that would keep one alive.

So the base is copied into our own registry under a dated tag, which
nobody deletes, and every FROM names that copy by digest:

    FROM ghcr.io/aareman/kosher-base-fedora-bootc:44-20261029@sha256:...

The copy keeps Fedora's digests exactly (--preserve-digests), so the pin is
Fedora's build, only hosted where it stays. Packages CI makes here are
public, so any machine can pull it with no login.

    scripts/bump-base.py            copy today's Fedora images, pin to them
    scripts/bump-base.py --dry-run  say what it would copy and pin

Copying needs push access to the registry: CI has it (bump-base.yml, every
Monday, in a pull request the merge train tests), a developer usually not.
"""

from __future__ import annotations

import argparse
import datetime
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONTAINERFILE = ROOT / "os-image/Containerfile"
UPSTREAM = {"fedora": "quay.io/fedora/fedora:44",
            "fedora-bootc": "quay.io/fedora/fedora-bootc:44"}
# A FROM of Fedora's image, or of our copy of it, with or without a digest.
FROM = re.compile(
    r"^(FROM\s+)(?:quay\.io/fedora/(fedora(?:-bootc)?):44"
    r"|ghcr\.io/[^/\s]+/kosher-base-(fedora(?:-bootc)?):44-\d{8})"
    r"(@sha256:[0-9a-f]{64})?(\s|$)",
    re.MULTILINE)


def owner() -> str:
    return (os.environ.get("GITHUB_REPOSITORY_OWNER") or "aareman").lower()


def mirror_of(name: str, date: str) -> str:
    return f"ghcr.io/{owner()}/kosher-base-{name}:44-{date}"


def skopeo() -> list[str]:
    if shutil.which("skopeo"):
        return ["skopeo"]
    return ["nix", "run", "nixpkgs#skopeo", "--"]


def digest(ref: str, run=subprocess.run) -> str:
    """The digest a reference points at: the multi-arch index's, so the
    same pin builds on x86_64 and aarch64."""
    res = run([*skopeo(), "inspect", "--format", "{{.Digest}}", f"docker://{ref}"],
              capture_output=True, text=True, timeout=300)
    out = (res.stdout or "").strip().splitlines()
    if res.returncode != 0 or not out or not re.fullmatch(r"sha256:[0-9a-f]{64}", out[-1]):
        raise SystemExit(f"could not read the digest of {ref}: {(res.stderr or '').strip()}")
    return out[-1]


def copy(source: str, dest: str, run=subprocess.run) -> None:
    """Every architecture, byte for byte, so the digest stays Fedora's."""
    res = run([*skopeo(), "copy", "--all", "--preserve-digests", "--retry-times", "3",
               f"docker://{source}", f"docker://{dest}"],
              capture_output=True, text=True, timeout=3600)
    if res.returncode != 0:
        raise SystemExit(f"could not copy {source} to {dest}: {(res.stderr or '').strip()}")


def pinned(text: str, refs: dict[str, str]) -> str:
    """Every FROM of a Fedora base names our copy: refs maps "fedora" and
    "fedora-bootc" to "ghcr.io/...:44-DATE@sha256:..."."""
    def to_copy(m):
        name = m.group(2) or m.group(3)
        return f"{m.group(1)}{refs[name]}{m.group(5)}"
    return FROM.sub(to_copy, text)


def main(argv=None, run=subprocess.run, today=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--dry-run", action="store_true", help="say what would be copied and pinned")
    args = p.parse_args(argv)
    text = CONTAINERFILE.read_text()
    names = sorted({m.group(2) or m.group(3) for m in FROM.finditer(text)})
    if not names:
        raise SystemExit(f"no fedora base image found in {CONTAINERFILE}")
    date = today or datetime.date.today().strftime("%Y%m%d")
    refs = {}
    for name in names:
        dest = mirror_of(name, date)
        if args.dry_run:
            print(f"would copy {UPSTREAM[name]} to {dest}")
            continue
        copy(UPSTREAM[name], dest, run=run)
        refs[name] = f"{dest}@{digest(dest, run=run)}"
        print(refs[name])
    if args.dry_run:
        return 0
    new = pinned(text, refs)
    if new == text:
        print("the pins are current")
        return 0
    CONTAINERFILE.write_text(new)
    print(f"pinned in {CONTAINERFILE.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
