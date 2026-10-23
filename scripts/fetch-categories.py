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
    scripts/fetch-categories.py --check PATH        # an existing catalogue
    scripts/fetch-categories.py --check-image IMAGE # the one inside an image

The build fails when a category a family can switch on comes out empty or
short (FLOORS). On 2026-10-21 the UT1 site answered 404 for every list,
the script shrugged, and two :edge images shipped with no adult, gambling
or proxy lists at all (#202). Now the lists come from UT1 or from their
GitHub mirror, and an image is built only when both are there in full.
"""

from __future__ import annotations

import argparse
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

SOURCE = "https://dsi.ut-capitole.fr/blacklists/download/{name}.tar.gz"

# The same lists, pushed to GitHub by a daily job (olbat/ut1-blacklists).
# Read when the primary fails. Pinned to a commit so a build cannot pick
# up whatever the mirror holds that morning; move the pin forward when the
# primary has been down for a while and the lists are ageing. A list
# larger than GitHub's file limit is kept there in 50 MB pieces
# (domains.0, domains.1, ...), which is how the adult list is stored.
MIRROR = ("https://raw.githubusercontent.com/olbat/ut1-blacklists/{ref}/"
          "blacklists/{name}/{file}")
MIRROR_REF = "03501c64d230df9d84124176558d758351027be1"  # 2026-10-12

# The primary serves some lists under a second name; the mirror has only
# the real directory. Both names feed the same KosherOS category, so when
# the mirror is in use the alias adds nothing and is skipped.
MIRROR_ALIASES = {
    "porn": "adult",
    "ads": "publicite",
    "proxy": "redirector",
    "drugs": "drogue",
    "violence": "agressif",
    "aggressive": "agressif",
}

# The fewest domains a category may ship with. These are the categories
# the admin app offers (kosherd.categories.CATEGORY_LABELS); each floor is
# about half of what the lists held on 2026-10-14, noted beside it, so the
# lists can be pruned upstream and the build still passes, while an empty
# or truncated category fails it. A category with no floor here is a
# catalogue-internal bucket the admin app never offers (jobsearch, manga,
# radio, ...): it is imported when its list is there and logged either way.
FLOORS = {
    "adult": 2_000_000,  # 4,599,409
    "malware": 100_000,  # 256,758
    "proxy": 50_000,  # 139,895
    "ads": 30_000,  # 64,928
    "gambling": 20_000,  # 48,718
    "shopping": 15_000,  # 36,972
    "games": 15_000,  # 33,743
    "dating": 4_000,  # 10,552
    "news": 2_000,  # 5,760
    "sports": 1_000,  # 2,389
    "social": 500,  # 1,160
    "immodest": 300,  # 828
    "drugs": 250,  # 603
    "violence": 150,  # 395
    "video": 10,  # 18, all from the curated supplement
}

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


def get(url: str, tries: int = 3) -> bytes:
    """The body at url. A 404 is final; anything else gets three tries,
    because the build now fails on a missing list and a dropped connection
    on a CI runner is not a reason to fail it."""
    for attempt in range(1, tries + 1):
        try:
            with urllib.request.urlopen(url, timeout=120) as response:
                return response.read()
        except urllib.error.HTTPError as e:
            if e.code == 404 or attempt == tries:
                raise
            error: Exception = e
        except Exception as e:  # noqa: BLE001 - timeouts, resets, DNS
            if attempt == tries:
                raise
            error = e
        print(f"    retrying after: {error}", file=sys.stderr)
        time.sleep(5 * attempt)
    raise AssertionError("unreachable")


def fetch_plain(url: str) -> str | None:
    """One advertising list, or None. A single ad list may be missing: the
    ads category has a dozen sources and its floor says whether enough of
    them came through."""
    try:
        return get(url).decode("utf-8", "ignore")
    except Exception as e:  # noqa: BLE001
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


def fetch_primary(name: str, workdir: Path) -> list[str] | None:
    """The list from UT1's own site, or None when it cannot be had."""
    target = workdir / f"{name}.tar.gz"
    try:
        target.write_bytes(get(SOURCE.format(name=name)))
        return domains_in(target, name)
    except Exception as e:  # noqa: BLE001
        print(f"  ! {name}: {e}", file=sys.stderr)
        return None
    finally:
        target.unlink(missing_ok=True)


def fetch_mirror(name: str, ref: str = MIRROR_REF) -> list[str] | None:
    """The list from the GitHub mirror: one `domains` file, or the pieces
    `domains.0`, `domains.1`, ... that a big list is split into. None when
    the mirror has neither."""
    real = MIRROR_ALIASES.get(name)
    if real:
        print(f"    {name} is {real} under another name; already imported")
        return []
    found: list[str] = []
    candidates = ["domains"] + [f"domains.{i}" for i in range(100)]
    for file in candidates:
        url = MIRROR.format(ref=ref, name=name, file=file)
        try:
            body = get(url)
        except urllib.error.HTTPError as e:
            if e.code != 404:
                print(f"  ! {name}: {e}", file=sys.stderr)
                return None
            if file == "domains":
                continue  # a split list has no plain file
            break  # past the last piece
        except Exception as e:  # noqa: BLE001
            print(f"  ! {name}: {e}", file=sys.stderr)
            return None
        found.extend(line.strip().lower()
                     for line in body.decode("utf-8", "ignore").splitlines())
        if file == "domains":
            break
    if not found:
        print(f"  ! {name}: not on the mirror at {ref[:12]}", file=sys.stderr)
        return None
    return found


def ut1_domains(name: str, workdir: Path, mirror_ref: str = MIRROR_REF) -> list[str]:
    """One UT1 list, from the primary or else the mirror. Empty when
    neither has it; whether that fails the build is the category floor's
    decision, not this function's."""
    domains = fetch_primary(name, workdir)
    if domains is None:
        print(f"    reading {name} from the GitHub mirror instead", flush=True)
        domains = fetch_mirror(name, mirror_ref)
    return domains or []


def domains_in(archive: Path, name: str) -> list[str]:
    """Every domain in the archive's `domains` file.

    The directory inside an archive is not always the name it was fetched
    under — `ads.tar.gz` unpacks to `publicite/`. Looking for whatever
    member ends in /domains avoids depending on that.
    """
    with tarfile.open(archive) as tar:
        path = next((m for m in tar.getnames()
                     if m.endswith("/domains") or m == "domains"), None)
        if path is None:
            raise ValueError("no domains file in the archive")
        member = tar.extractfile(path)
        if member is None:
            return []
        return [line.decode("utf-8", "ignore").strip().lower()
                for line in member]


CURATED_PATH = Path(__file__).with_name("curated-sites.json")

# Video-call services (kosherd.categories.CALLS): a catalogue bucket, never
# a toggle. The curated hosts in it lose every other classification but
# these, the ones that say a host is dangerous rather than what it is for.
CALLS = "calls"
CALLS_KEEP = ("ads", "malware", "adult", "proxy")


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


def build(out: Path, only: set[str] | None, mirror_ref: str = MIRROR_REF) -> int:
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
            rows = [(d, category)
                    for d in ut1_domains(source_name, workdir, mirror_ref)
                    if d and "." in d and " " not in d]
            db.executemany(
                "INSERT OR IGNORE INTO domains (domain, category) VALUES (?, ?)",
                rows)
            db.commit()
            total += len(rows)
            print(f"    {len(rows):,}")

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
    # Whatever --only says: a partial build of social must not put the
    # call services back in it.
    calls = sorted({host for host, category in curated_rows(None)
                    if category == CALLS})
    curated += [(host, CALLS) for host in calls if (host, CALLS) not in curated]
    if calls:
        # A call service is in no category a family switches off (#209):
        # its taste rows go, for it and its subdomains, before it is filed
        # under calls. What marks a host as dangerous stays.
        where = " OR ".join("domain = ? OR domain LIKE ?" for _ in calls)
        params = [p for host in calls for p in (host, "%." + host)]
        kept = ", ".join("?" for _ in CALLS_KEEP)
        removed = db.execute(
            f"DELETE FROM domains WHERE ({where}) AND category NOT IN ({kept})",
            params + list(CALLS_KEEP)).rowcount
        db.commit()
        print(f"  call services -> {len(calls)} hosts, {removed} rows "
              "reclassified", flush=True)
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


def counts_in(path: Path) -> dict[str, int]:
    """Domains per category in a catalogue."""
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return dict(db.execute(
            "SELECT category, COUNT(*) FROM domains GROUP BY category"))
    finally:
        db.close()


def shortfalls(counts: dict[str, int], only: set[str] | None = None) -> dict[str, int]:
    """The categories under their floor, with what they hold."""
    return {category: counts.get(category, 0)
            for category, floor in FLOORS.items()
            if (not only or category in only) and counts.get(category, 0) < floor}


def report(counts: dict[str, int], only: set[str] | None = None) -> bool:
    """Print every category's count against its floor; True when all the
    required ones clear it. This is the table the build log shows."""
    short = shortfalls(counts, only)
    print()
    print(f"  {'category':18} {'domains':>10}   {'floor':>10}")
    for category, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        floor = FLOORS.get(category)
        mark = ("SHORT" if category in short else "ok") if floor else "-"
        print(f"  {category:18} {n:>10,}   {floor or 0:>10,}  {mark}")
    for category in short:
        if category not in counts:
            print(f"  {category:18} {0:>10,}   {FLOORS[category]:>10,}  SHORT")
    sys.stdout.flush()  # the table before the verdict, even when both go to one log
    if short:
        print("\nthe catalogue is incomplete; these categories are below "
              "their floor: " + ", ".join(f"{c} ({n:,})" for c, n in short.items()),
              file=sys.stderr)
        return False
    print(f"\nevery required category is above its floor ({len(FLOORS)} checked)")
    return True


def check_image(image: str) -> bool:
    """The catalogue inside a built image, held to the same floors. Cheap:
    one container created and removed, one file copied out."""
    container = subprocess.run(["podman", "create", image], check=True,
                               capture_output=True, text=True).stdout.strip()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "categories.sqlite"
            subprocess.run(["podman", "cp",
                            f"{container}:/usr/share/kosher/categories/categories.sqlite",
                            str(target)], check=True)
            print(f"checking the catalogue in {image}")
            return report(counts_in(target))
    finally:
        subprocess.run(["podman", "rm", container], check=False,
                       capture_output=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="os-image/files/usr/share/kosher/"
                                         "categories/categories.sqlite")
    parser.add_argument("--only", help="comma-separated KosherOS categories")
    parser.add_argument("--mirror-ref", default=MIRROR_REF,
                        help="commit of olbat/ut1-blacklists to read when UT1 is down")
    parser.add_argument("--check", metavar="PATH",
                        help="hold an existing catalogue to the floors instead of building")
    parser.add_argument("--check-image", metavar="IMAGE",
                        help="hold the catalogue inside a built image to the floors")
    args = parser.parse_args(argv)

    if args.check:
        print(f"checking {args.check}")
        return 0 if report(counts_in(Path(args.check))) else 1
    if args.check_image:
        return 0 if check_image(args.check_image) else 1

    only = set(args.only.split(",")) if args.only else None
    out = Path(args.out)
    print(f"building {out}")
    total = build(out, only, args.mirror_ref)
    size_mb = out.stat().st_size / 1_000_000 if out.exists() else 0
    print(f"\n{total:,} entries, {size_mb:.1f} MB at {out}")
    if not report(counts_in(out), only):
        return 1
    return 0 if total else 1


if __name__ == "__main__":
    sys.exit(main())
