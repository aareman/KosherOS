"""Keeping the five-million-domain list current.

The category database is the backbone of the filter and the part that goes
stale fastest: domains are registered daily, and a list frozen at the day
the image was built is worth less every week. It is also 190 MB, so it
cannot ride inside a signed JSON document the way the word lists do.

So the portal signs a MANIFEST — a URL, a size, and a SHA-256 — and the
device fetches the file itself and checks it against that hash before
letting it anywhere near the filter. The signature covers the hash, so the
download needs no trust of its own: it can come from a CDN, over plain
HTTP, from a mirror, and a byte out of place is caught here.

Replaced atomically, and only after it opens and answers a query. A
truncated database that loaded would be worse than no update at all,
because it would look like it was working.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

from . import lists

log = logging.getLogger(__name__)

CATALOG_PATH = lists.OVERRIDE_DIR / "categories" / "categories.sqlite"
VERSION_PATH = lists.OVERRIDE_DIR / "categories" / "version.json"

DOWNLOAD_TIMEOUT = 300
# Generous, because this is a database of every classified domain on the
# web; small enough that a wrong URL cannot fill the disk.
MAX_BYTES = 1024 * 1024 * 1024
CHUNK = 1024 * 1024
# Below this a "database" is a redirect page or an error, not a catalogue.
MIN_DOMAINS = 100_000


class CatalogError(Exception):
    pass


def installed_version() -> int:
    try:
        return int(json.loads(VERSION_PATH.read_text())["version"])
    except (OSError, ValueError, KeyError, TypeError):
        return 0


def download(url: str, expected_sha: str, expected_size: int | None = None,
             *, timeout: int = DOWNLOAD_TIMEOUT) -> Path:
    """Fetch the file and return a temporary path, or raise.

    The hash is checked as the bytes arrive rather than afterwards, so a
    mismatch costs one wasted download and never a wasted disk.
    """
    digest = hashlib.sha256()
    size = 0
    CATALOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    handle, tmp_name = tempfile.mkstemp(dir=CATALOG_PATH.parent,
                                        suffix=".download")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(handle, "wb") as out:
            with urllib.request.urlopen(url, timeout=timeout) as response:
                while True:
                    chunk = response.read(CHUNK)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > MAX_BYTES:
                        raise CatalogError("the catalogue is implausibly large")
                    digest.update(chunk)
                    out.write(chunk)
    except (urllib.error.URLError, OSError, ValueError) as e:
        tmp.unlink(missing_ok=True)
        raise CatalogError(f"could not download the catalogue: {e}") from None
    except CatalogError:
        tmp.unlink(missing_ok=True)
        raise

    if digest.hexdigest() != expected_sha.lower():
        tmp.unlink(missing_ok=True)
        raise CatalogError("the catalogue does not match the signed hash")
    if expected_size is not None and size != expected_size:
        tmp.unlink(missing_ok=True)
        raise CatalogError(
            f"the catalogue is {size} bytes, not the signed {expected_size}")
    return tmp


def usable(path: Path) -> int:
    """How many domains this file holds, or raise if it is not a catalogue.

    Opened and queried before it is installed. A truncated database that
    loaded anyway would be worse than no update, because it would look
    like it was working.
    """
    from . import categories

    try:
        bundle = categories.SqliteBundle(path)
        count = len(bundle)
    except Exception as e:  # noqa: BLE001 - anything here means "not a catalogue"
        raise CatalogError(f"the downloaded file is not usable: {e}") from None
    if count < MIN_DOMAINS:
        raise CatalogError(
            f"the downloaded catalogue holds only {count} domains")
    return count


def install(tmp: Path, version: int) -> int:
    """Put a verified catalogue in place, atomically."""
    count = usable(tmp)
    tmp.chmod(0o644)
    tmp.replace(CATALOG_PATH)
    VERSION_PATH.write_text(
        json.dumps({"version": int(version), "domains": count}) + "\n")
    VERSION_PATH.chmod(0o644)
    log.info("installed catalogue v%s with %d domains", version, count)
    return count


def update(manifest: dict) -> int | None:
    """Apply a verified manifest. Returns the domain count, or None.

    `manifest` has already had its signature checked against the enrolled
    portal key; what is checked here is that the bytes it points at are
    the bytes it promised.
    """
    version = manifest.get("version")
    url = manifest.get("url")
    sha = manifest.get("sha256")
    if not isinstance(version, int) or not url or not sha:
        raise CatalogError("the catalogue manifest is incomplete")
    if version <= installed_version():
        return None
    tmp = download(url, sha, manifest.get("size"))
    try:
        return install(tmp, version)
    except CatalogError:
        tmp.unlink(missing_ok=True)
        raise
