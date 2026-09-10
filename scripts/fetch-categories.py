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
    # Torrent and warez sites: a filter that misses them leaks everything
    # else, since what arrives through them is not filtered at all.
    "warez": "filehosting",
    "tricheur": "cheating",
    "dangerous_material": "violence",
    "marketingware": "ads",
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

# Plain domain lists on top of UT1, per KosherOS category. These are what
# make "block ads everywhere" a Pi-hole rather than a gesture: UT1's own
# advertising lists are a few thousand domains, and these add sixty-five
# thousand more. Everything here lands in "ads", which dns.py turns into
# NXDOMAIN for every account on the machine — so a scam or malware domain
# in this set is blocked more thoroughly than one reached through a
# per-user category. Hosts-file or one-domain-per-line format; comments and
# the loopback boilerplate are dropped.
#
# These are fetched INDIVIDUALLY rather than through StevenBlack's merged
# unified-hosts file, which is what this used to do, and the reason is
# licensing. That file is an aggregate of sixteen lists; the MIT licence
# covers Steven Black's own wrapper, not the merged content. Two of the
# sixteen — MVPS (CC BY-NC-SA 4.0) and someonewhocares (non-commercial
# with attribution) — forbid commercial use, and our category database is
# an adaptation of UT1's CC BY-SA 4.0 data. ShareAlike forces the whole
# database to be licensed BY-SA 4.0, which *permits* commercial use, so
# nothing forbidding it can be inside. Merging those two produced a file
# that could not be licensed at all. And once merged there is no way to
# tell which domains came from where, so they could not be stripped later.
#
# Dropping them costs 18,986 domains (23.7% of the merged file) and is not
# optional. Fetching the rest directly gains 4,770, because these are live
# upstreams rather than a snapshot. Net: 79,962 -> 65,746.
#
# Every licence below is from the source's own update.json. Attribution is
# required by several of them; see THIRD-PARTY.md.
PLAIN_SOURCES = {
    "ads": (
        ("Steven Black's ad-hoc list (MIT)",
         "https://raw.githubusercontent.com/StevenBlack/hosts/master/data/StevenBlack/hosts"),
        ("AdAway (CC BY 3.0)",
         "https://raw.githubusercontent.com/AdAway/adaway.github.io/master/hosts.txt"),
        ("add.2o7Net (MIT)",
         "https://raw.githubusercontent.com/FadeMind/hosts.extras/master/add.2o7Net/hosts"),
        ("add.Dead (MIT)",
         "https://raw.githubusercontent.com/FadeMind/hosts.extras/master/add.Dead/hosts"),
        ("add.Risk (MIT)",
         "https://raw.githubusercontent.com/FadeMind/hosts.extras/master/add.Risk/hosts"),
        ("add.Spam (MIT)",
         "https://raw.githubusercontent.com/FadeMind/hosts.extras/master/add.Spam/hosts"),
        ("UncheckyAds (MIT)",
         "https://raw.githubusercontent.com/FadeMind/hosts.extras/master/UncheckyAds/hosts"),
        ("Mitchell Krog's Badd Boyz Hosts (MIT)",
         "https://raw.githubusercontent.com/mitchellkrogza/Badd-Boyz-Hosts/master/hosts"),
        ("hostsVN (MIT)",
         "https://raw.githubusercontent.com/bigdargon/hostsVN/master/option/hosts-VN"),
        ("KADhosts (CC BY-SA 4.0)",
         "https://raw.githubusercontent.com/FiltersHeroes/KADhosts/master/KADhosts.txt"),
        ("minecraft-hosts (CC0-1.0)",
         "https://raw.githubusercontent.com/jamiemansfield/minecraft-hosts/master/lists/tracking.txt"),
        ("tiuxo hostlist - ads (CC BY 4.0)",
         "https://raw.githubusercontent.com/tiuxo/hosts/master/ads"),
        ("URLhaus (CC0)",
         "https://urlhaus.abuse.ch/downloads/hostfile/"),
        # No formal licence, but the author grants this use in as many
        # words: "Feel free to combine this list with yours or lists from
        # other sites and put it up on the web, though!" Nothing there
        # restricts commercial use, so it can live in a BY-SA database.
        ("Peter Lowe's list, yoyo.org (permission granted on the page)",
         "https://pgl.yoyo.org/adservers/serverlist.php"
         "?hostformat=hosts&mimetype=plaintext&useip=0.0.0.0"),
    ),
}
# Names a hosts file carries that are not domains to block.
HOSTS_NOISE = {"localhost", "localhost.localdomain", "local", "broadcasthost",
               "ip6-localhost", "ip6-loopback", "ip6-localnet", "ip6-mcastprefix",
               "ip6-allnodes", "ip6-allrouters", "ip6-allhosts", "0.0.0.0"}


def plain_domains(text: str) -> list[str]:
    """Domains from a hosts file or a one-per-line list."""
    found = []
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip().lower()
        if not line:
            continue
        parts = line.split()
        # "0.0.0.0 example.com" or "127.0.0.1 example.com" or bare "example.com".
        domain = parts[-1] if len(parts) > 1 else parts[0]
        if domain in HOSTS_NOISE or "." not in domain or "/" in domain:
            continue
        found.append(domain.strip("."))
    return found


def fetch_plain(url: str) -> str | None:
    try:
        with urllib.request.urlopen(url, timeout=120) as response:
            return response.read().decode("utf-8", "ignore")
    except Exception as e:  # noqa: BLE001 - one missing list must not stop the build
        print(f"  ! {url}: {e}", file=sys.stderr)
        return None


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
    """Every domain in the archive's `domains` file.

    The directory inside an archive is not always the name it was fetched
    under — `ads.tar.gz` unpacks to `publicite/`. Looking for whatever
    member ends in /domains avoids depending on that, and one archive that
    does not match must never abort the whole import.
    """
    try:
        with tarfile.open(archive) as tar:
            path = next((m for m in tar.getnames()
                         if m.endswith("/domains") or m == "domains"), None)
            if path is None:
                print(f"  ! {name}: no domains file in the archive",
                      file=sys.stderr)
                return []
            member = tar.extractfile(path)
            if member is None:
                return []
            return [line.decode("utf-8", "ignore").strip().lower()
                    for line in member]
    except Exception as e:  # noqa: BLE001 - one bad archive must not stop the build
        print(f"  ! {name}: {e}", file=sys.stderr)
        return []


CURATED_PATH = Path(__file__).with_name("curated-sites.json")


def curated_rows(only: set[str] | None) -> list[tuple[str, str]]:
    """Mainstream sites the UT1 lists miss, so a family's real categories are
    complete out of the box. UT1 is deep but patchy on the obvious names:
    it has thousands of sports blogs and not espn.com, nba.com or
    skysports.com. This fills that in for the categories a family reaches."""
    if not CURATED_PATH.exists():
        return []
    import json

    data = json.loads(CURATED_PATH.read_text())
    rows = []
    for category, sites in data.items():
        if category.startswith("_"):
            continue
        if only and category not in only:
            continue
        for site in sites:
            host = site.split("/", 1)[0].strip().lower()  # drop any path
            if host and "." in host and " " not in host:
                rows.append((host, category))
    return rows


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
            try:
                rows = [(d, category) for d in domains_in(archive, source_name)
                        if d and "." in d and " " not in d]
                db.executemany(
                    "INSERT OR IGNORE INTO domains (domain, category) VALUES (?, ?)",
                    rows)
                db.commit()
                total += len(rows)
                print(f"    {len(rows):,}")
            except Exception as e:  # noqa: BLE001
                print(f"  ! {source_name}: {e}", file=sys.stderr)
            finally:
                archive.unlink(missing_ok=True)

    for category, sources in sorted(PLAIN_SOURCES.items()):
        if only and category not in only:
            continue
        for label, url in sources:
            print(f"  {label} -> {category}", flush=True)
            text = fetch_plain(url)
            if text is None:
                continue
            rows = [(d, category) for d in plain_domains(text)]
            db.executemany(
                "INSERT OR IGNORE INTO domains (domain, category) VALUES (?, ?)",
                rows)
            db.commit()
            total += len(rows)
            print(f"    {len(rows):,}")

    curated = curated_rows(only)
    if curated:
        # OR REPLACE, not OR IGNORE: a curated classification is deliberate
        # and should win over a UT1 row that put the same domain elsewhere.
        db.executemany(
            "INSERT OR REPLACE INTO domains (domain, category) VALUES (?, ?)",
            curated)
        db.commit()
        total += len(curated)
        print(f"  curated supplement -> {len(curated):,} mainstream sites",
              flush=True)

    db.execute("INSERT OR REPLACE INTO meta VALUES ('version', ?)",
               (time.strftime("%Y-%m-%d"),))
    # Attribution, not decoration: UT1 is CC BY-SA 4.0 and several of the
    # advertising lists are CC BY, so naming them is a licence condition and
    # this string is where the product does it. The admin app shows it.
    db.execute("INSERT OR REPLACE INTO meta VALUES ('source', ?)",
               ("University of Toulouse (UT1) blacklists, CC BY-SA 4.0; "
                "advertising and tracker lists from AdAway, KADhosts, tiuxo, "
                "hostsVN, Badd Boyz, FadeMind, minecraft-hosts, URLhaus, "
                "yoyo.org and Steven Black; plus KosherOS curated. "
                "See THIRD-PARTY.md",))
    db.commit()

    # Guard against shipping a catalogue with no version/source. This is not
    # paranoia: a dropped trailing comma once turned the source INSERT's
    # parameter tuple into a bare string, the statement raised, and the
    # catalogue shipped with empty meta — kosherd then logged "version 0"
    # and the admin app had no version to show. Fail the build instead.
    meta = dict(db.execute("SELECT key, value FROM meta").fetchall())
    missing = {"version", "source"} - set(meta)
    if missing:
        db.close()
        raise SystemExit(f"catalogue meta is incomplete, missing {sorted(missing)}")

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
