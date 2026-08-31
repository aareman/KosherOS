#!/usr/bin/env python3
"""Build the shipped category database from established public lists.

A family must not have to assemble lists. This imports the University of
Toulouse (UT1) blacklists — maintained for two decades, used across French
schools and universities, and freely redistributable — and maps their
categories onto the ones KosherOS exposes.

The output is SQLite, not JSON, for a specific reason: the adult list alone
is millions of domains, and holding that in a Python dict would cost
hundreds of megabytes of RAM on exactly the low-end family machine this has
to run on. SQLite keeps it on disk, answers a lookup in microseconds, and
costs a few megabytes of page cache.

    scripts/fetch-categories.py [--out PATH] [--only adult,gambling]
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import tarfile
import tempfile
import time
import urllib.request
from pathlib import Path

SOURCE = "https://dsi.ut-capitole.fr/blacklists/download/{name}.tar.gz"

# UT1 category -> the KosherOS category it feeds. Several UT1 lists collapse
# into one of ours; a few are deliberately left out (see below).
MAPPING = {
    "adult": "adult",
    "porn": "adult",
    "mixed_adult": "adult",
    "dating": "dating",
    "lingerie": "immodest",
    "celebrity": "immodest",
    "gambling": "gambling",
    "games": "games",
    "shopping": "shopping",
    "social_networks": "social",
    "chat": "social",
    "forums": "social",
    "press": "news",
    "fakenews": "news",
    "ads": "ads",
    "publicite": "ads",
    "malware": "malware",
    "phishing": "malware",
    "cryptojacking": "malware",
    "ddos": "malware",
    "hacking": "malware",
    "stalkerware": "malware",
    "dialer": "malware",
    "proxy": "proxy",
    "redirector": "proxy",
    "strict_redirector": "proxy",
    "strong_redirector": "proxy",
    "shortener": "proxy",
    # DNS-over-HTTPS providers: reaching one is how a filter gets bypassed.
    "doh": "proxy",
    "drugs": "drugs",
    "drogue": "drugs",
    "violence": "violence",
    "aggressive": "violence",
    "agressif": "violence",
    "sect": "sect",
    "sexual_education": "sexual_education",
    "astrology": "astrology",
    "bitcoin": "financial",
    "bank": "financial",
    "filehosting": "filehosting",
    "download": "filehosting",
    "manga": "manga",
    "sports": "sports",
    "radio": "radio",
    "blog": "blog",
    "jobsearch": "jobsearch",
    "translation": "translation",
}

# Deliberately not imported: "liste_blanche"/"listes_blanches" are
# allow-lists, "all"/"blacklists" are the whole corpus in one file, and the
# vendor-specific bundles are the same data in another format.

SCHEMA = """
CREATE TABLE IF NOT EXISTS domains (
    domain   TEXT NOT NULL,
    category TEXT NOT NULL,
    PRIMARY KEY (domain, category)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


def fetch(name: str, workdir: Path) -> Path | None:
    url = SOURCE.format(name=name)
    target = workdir / f"{name}.tar.gz"
    try:
        with urllib.request.urlopen(url, timeout=120) as response:
            target.write_bytes(response.read())
    except Exception as e:  # noqa: BLE001 - one missing list must not stop the build
        print(f"  ! {name}: {e}", file=sys.stderr)
        return None
    return target


def domains_in(archive: Path, name: str) -> list[str]:
    try:
        with tarfile.open(archive) as tar:
            member = tar.extractfile(f"{name}/domains")
            if member is None:
                return []
            return [line.decode("utf-8", "ignore").strip().lower()
                    for line in member]
    except (tarfile.TarError, OSError) as e:
        print(f"  ! {name}: {e}", file=sys.stderr)
        return []


def build(out: Path, only: set[str] | None) -> int:
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        out.unlink()
    db = sqlite3.connect(out)
    db.executescript(SCHEMA)
    # Bulk load: durability does not matter for a file we can rebuild.
    db.execute("PRAGMA journal_mode = OFF")
    db.execute("PRAGMA synchronous = OFF")

    total = 0
    with tempfile.TemporaryDirectory() as tmp:
        workdir = Path(tmp)
        for source_name, category in sorted(MAPPING.items()):
            if only and category not in only:
                continue
            print(f"  {source_name} -> {category}", flush=True)
            archive = fetch(source_name, workdir)
            if archive is None:
                continue
            rows = [(d, category) for d in domains_in(archive, source_name)
                    if d and "." in d and " " not in d]
            db.executemany(
                "INSERT OR IGNORE INTO domains (domain, category) VALUES (?, ?)",
                rows)
            db.commit()
            total += len(rows)
            archive.unlink()

    db.execute("INSERT OR REPLACE INTO meta VALUES ('version', ?)",
               (time.strftime("%Y-%m-%d"),))
    db.execute("INSERT OR REPLACE INTO meta VALUES ('source', ?)",
               ("University of Toulouse (UT1) blacklists",))
    db.commit()
    db.execute("VACUUM")
    db.close()
    return total


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="os-image/files/usr/share/kosher/"
                                         "categories/categories.sqlite")
    parser.add_argument("--only", help="comma-separated KosherOS categories")
    args = parser.parse_args()

    only = set(args.only.split(",")) if args.only else None
    out = Path(args.out)
    print(f"building {out}")
    total = build(out, only)
    size_mb = out.stat().st_size / 1_000_000 if out.exists() else 0
    print(f"\n{total:,} entries, {size_mb:.1f} MB at {out}")
    return 0 if total else 1


if __name__ == "__main__":
    sys.exit(main())
