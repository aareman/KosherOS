#!/usr/bin/env python3
"""Pin the Containerfile's base images to their current digests.

The image is built FROM quay.io/fedora/fedora-bootc:44 (and its builder
stages FROM quay.io/fedora/fedora:44). Those tags move about once a day,
and every move changes the cache key of every layer after it: the next
build started from nothing, twenty-two minutes of it, to change a line of
Python. So each FROM names a digest, which moves only when this script
moves it (`just bump-base`, and weekly by .github/workflows/bump-base.yml,
whose pull request goes through the merge train like any other).

    scripts/bump-base.py            pin to what the tags point at now
    scripts/bump-base.py --check    exit 1 if the pins are behind
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONTAINERFILE = ROOT / "os-image/Containerfile"
# Each FROM of one of these tags, with or without a digest already.
FROM = re.compile(r"^(FROM\s+)(quay\.io/fedora/fedora(?:-bootc)?:44)(@sha256:[0-9a-f]{64})?(\s|$)",
                  re.MULTILINE)


def skopeo() -> list[str]:
    if shutil.which("skopeo"):
        return ["skopeo"]
    return ["nix", "run", "nixpkgs#skopeo", "--"]


def digest(ref: str, run=subprocess.run) -> str:
    """The digest the tag points at now: the multi-arch index's, so the same
    pin builds on x86_64 and aarch64."""
    res = run([*skopeo(), "inspect", "--format", "{{.Digest}}", f"docker://{ref}"],
              capture_output=True, text=True, timeout=300)
    out = (res.stdout or "").strip().splitlines()
    if res.returncode != 0 or not out or not re.fullmatch(r"sha256:[0-9a-f]{64}", out[-1]):
        raise SystemExit(f"could not read the digest of {ref}: {(res.stderr or '').strip()}")
    return out[-1]


def pinned(text: str, digests: dict[str, str]) -> str:
    return FROM.sub(lambda m: f"{m.group(1)}{m.group(2)}@{digests[m.group(2)]}{m.group(4)}", text)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--check", action="store_true", help="only say whether the pins are current")
    args = p.parse_args(argv)
    text = CONTAINERFILE.read_text()
    refs = sorted({m.group(2) for m in FROM.finditer(text)})
    if not refs:
        raise SystemExit(f"no fedora base image found in {CONTAINERFILE}")
    digests = {ref: digest(ref) for ref in refs}
    new = pinned(text, digests)
    for ref, d in digests.items():
        print(f"{ref}@{d}")
    if new == text:
        print("the pins are current")
        return 0
    if args.check:
        print("the pins are behind: run `just bump-base`")
        return 1
    CONTAINERFILE.write_text(new)
    print(f"pinned in {CONTAINERFILE.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
