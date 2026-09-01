#!/usr/bin/env python3
"""Publish the filter lists, and a catalogue manifest, to a portal.

The device side of list updates is signed, verified and tested. Without
this it is also unusable, because nothing puts a bundle into the portal —
and a feature that only works if somebody hand-crafts JSON is a feature
that stays switched off.

    just publish-lists https://portal.example
    just publish-lists https://portal.example catalog-url=https://cdn/…

The lists come from this repository, which is the same place the image
gets them: publishing is "send what the next image would have shipped",
so a family on an old image catches up without waiting for one.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SHIPPED = REPO / "os-image/files/usr/share/kosher"

# Exactly the names the device will accept (see kosherd/lists.py). Sending
# anything else would be silently dropped on arrival, which is a worse
# outcome than refusing to send it.
LISTS = ("wordlist.json", "content-terms.json", "site-rules.json",
         "search-blocklist.json")


def read_lists() -> dict:
    documents = {}
    for name in LISTS:
        path = SHIPPED / name
        try:
            documents[name] = json.loads(path.read_text())
        except FileNotFoundError:
            print(f"  {name}: not in this repository, skipping",
                  file=sys.stderr)
        except ValueError as e:
            raise SystemExit(f"{path} is not valid JSON: {e}")
    return documents


def catalogue_manifest(url: str, local: Path, version: int) -> dict:
    """Hash the catalogue that `url` will serve.

    The hash is taken from the local file, so what is signed is what was
    built here — not whatever the URL happens to return at the moment of
    publishing, which is the mistake this would otherwise invite.
    """
    if not local.exists():
        raise SystemExit(f"no catalogue at {local}; build the image first, or "
                         "pass --catalog-file")
    digest = hashlib.sha256()
    size = 0
    with local.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
            size += len(chunk)
    return {"version": version, "url": url, "sha256": digest.hexdigest(),
            "size": size}


def publish(portal: str, token: str, body: dict) -> dict:
    request = urllib.request.Request(
        f"{portal.rstrip('/')}/api/v1/admin/lists",
        data=json.dumps(body).encode(), method="PUT",
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as e:
        raise SystemExit(f"portal refused the bundle: HTTP {e.code} "
                         f"{e.read().decode()[:300]}")
    except urllib.error.URLError as e:
        raise SystemExit(f"cannot reach {portal}: {e.reason}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("portal", help="portal base URL")
    parser.add_argument("--token", default=os.environ.get("KOSHER_PORTAL_ADMIN_TOKEN"),
                        help="admin token (or KOSHER_PORTAL_ADMIN_TOKEN)")
    parser.add_argument("--catalog-url",
                        help="where devices will download the category "
                             "database from")
    parser.add_argument("--catalog-file", type=Path,
                        default=REPO / "build/categories/categories.sqlite",
                        help="the catalogue to hash (must be the file that "
                             "URL will serve)")
    parser.add_argument("--catalog-version", type=int, default=1)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if not args.token and not args.dry_run:
        raise SystemExit("no admin token; set KOSHER_PORTAL_ADMIN_TOKEN")

    body = {"lists": read_lists()}
    for name, document in body["lists"].items():
        print(f"  {name}: {_entries(document)} entries")
    if args.catalog_url:
        body["catalog"] = catalogue_manifest(
            args.catalog_url, args.catalog_file, args.catalog_version)
        print(f"  catalogue: v{body['catalog']['version']}, "
              f"{body['catalog']['size'] / 1e6:.0f} MB, "
              f"sha256 {body['catalog']['sha256'][:16]}…")

    if args.dry_run:
        print("\ndry run; nothing was published")
        return 0
    result = publish(args.portal, args.token, body)
    print(f"\npublished bundle v{result['version']}: "
          f"{', '.join(result['lists'])}")
    return 0


def _entries(document) -> int:
    if isinstance(document.get("replacements"), dict):
        return len(document["replacements"])
    terms = document.get("terms")
    if isinstance(terms, list):
        return len(terms)
    if isinstance(terms, dict):
        return sum(len(words) for by_weight in terms.values()
                   for words in by_weight.values())
    return len(document.get("sites", []))


if __name__ == "__main__":
    raise SystemExit(main())
