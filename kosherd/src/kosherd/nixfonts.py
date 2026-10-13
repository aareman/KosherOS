"""Expose Nix profile fonts through the user's standard font directory.

Copy rather than symlink: Flatpak apps can read user fonts but cannot
necessarily follow a link into /nix/store. A manifest tracks only our
copies, so an upgrade/removal does not alter fonts the user added or edited.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

SUFFIXES = {".ttf", ".otf", ".ttc", ".otc"}


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def sync(source: Path | None, dest: Path) -> tuple[int, int]:
    manifest = dest / ".kosher-fonts.json"
    try:
        previous = json.loads(manifest.read_text())
    except (OSError, ValueError):
        previous = {}
    if not isinstance(previous, dict):
        previous = {}
    wanted = {}
    # Profiles often symlink whole directories, not just individual files.
    # Path.rglob does not traverse those directories on Python 3.12.
    if source and source.is_dir():
        for directory, dirs, files in os.walk(source, followlinks=True):
            root = Path(directory)
            ancestors = {p.resolve() for p in (root, *root.parents)}
            dirs[:] = [d for d in dirs if (root / d).resolve() not in ancestors]
            for name in files:
                font = root / name
                if font.is_file() and font.suffix.lower() in SUFFIXES:
                    wanted[str(font.relative_to(source))] = font.resolve()
    records = {}
    made = removed = 0
    for name in sorted(set(previous) | set(wanted)):
        relative = Path(name)
        if relative.is_absolute() or ".." in relative.parts:
            continue
        target = dest / relative
        old = previous.get(name) or {}
        font = wanted.get(name)
        if target.is_symlink():
            continue  # Not a copy made by this helper.
        if target.exists():
            if not isinstance(old, dict) or not old.get("sha256"):
                continue
            if font and old.get("source") == str(font):
                records[name] = old
                continue
            if digest(target) != old["sha256"]:
                continue  # The user edited this font; leave it alone.
            if font is None:
                target.unlink()
                removed += 1
                continue
        if font is None:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as tmp:
            temp = Path(tmp.name)
        try:
            shutil.copyfile(font, temp)
            temp.chmod(0o644)
            checksum = digest(temp)
            os.replace(temp, target)
        finally:
            temp.unlink(missing_ok=True)
        records[name] = {"source": str(font), "sha256": checksum}
        made += 1
    if records != previous:
        dest.mkdir(parents=True, exist_ok=True)
        temp_manifest = manifest.with_suffix(".tmp")
        temp_manifest.write_text(json.dumps(records, indent=2) + "\n")
        temp_manifest.replace(manifest)
    if made or removed:
        try:
            subprocess.run(["fc-cache", "-f", str(dest)], check=True,
                           capture_output=True, timeout=30)
        except (OSError, subprocess.SubprocessError) as error:
            logging.getLogger(__name__).warning("Could not refresh font cache: %s", error)
    return made, removed
