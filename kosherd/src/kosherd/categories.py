"""Category lists: blocking by what a site is, not just by its name.

Until now a site was allowed or blocked by identity — a whitelist, a DNS
blocklist, or a hand-written URL rule. That does not scale to the web, and
it is the gap between "blocks known adult domains" and a filter a family
actually trusts (see docs/content-filtering.md).

A category bundle maps domains to categories (adult, gambling, social,
video, …). Per-user policy then says which categories that person may not
reach. The bundle is data, not code: it ships with the image, and later
arrives signed from the portal, so lists improve without an OS release.

Matching is by domain suffix, because listing every subdomain is hopeless:
an entry for `example.com` also covers `cdn.example.com`. Longer entries
win, so a specific subdomain can be classified differently from its parent.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from dataclasses import dataclass, field
from . import lists
from pathlib import Path

log = logging.getLogger(__name__)

BUNDLE_DIR = Path("/usr/share/kosher/categories")
LOCAL_BUNDLE_DIR = lists.OVERRIDE_DIR / "categories"

# What each category means, for the admin UI. The wording matters: an admin
# is choosing on behalf of a family, so the label has to say what will
# actually stop working.
# The user-facing categories, in the order the admin app shows them. This
# is the authoritative set: only these are offered as filter toggles, so a
# family never sees raw UT1 buckets like "astrology", "blog" or "radio".
# Each maps a catalogue category to a label; the catalogue may hold more
# categories than this, and they are simply not surfaced.
CATEGORY_LABELS = {
    "adult": "Adult and pornography",
    "immodest": "Immodest imagery",
    "gambling": "Gambling and betting",
    "dating": "Dating and matchmaking",
    "social": "Social networks",
    "video": "Video and streaming",
    "sports": "Sports",
    "games": "Games",
    "shopping": "Shopping and marketplaces",
    "news": "News and media",
    "drugs": "Drugs",
    "violence": "Violence",
    "ads": "Advertising and trackers",
    "malware": "Malware and phishing",
    "proxy": "Proxies, VPNs and filter bypass",
}

# The order above is the display order; a category not in here is a
# catalogue-internal bucket, never shown as a filter toggle.
USER_FACING_CATEGORIES = tuple(CATEGORY_LABELS)

# Blocked by default for a newly filtered account. Deliberately narrow:
# categories that are about danger or immodesty rather than taste, so the
# default is defensible without an admin having to reason about it.
DEFAULT_BLOCKED = ("adult", "gambling", "dating", "malware", "proxy")


class CategoryError(Exception):
    pass


@dataclass
class Bundle:
    """A loaded category list."""

    version: str = "0"
    source: str = ""
    domains: dict[str, tuple[str, ...]] = field(default_factory=dict)

    def __len__(self) -> int:
        return len(self.domains)

    @property
    def categories(self) -> set[str]:
        return {c for cats in self.domains.values() for c in cats}

    def categories_of(self, host: str) -> set[str]:
        """Categories for a hostname, matching the longest domain suffix.

        `cdn.example.com` matches an entry for `example.com`; an entry for
        `cdn.example.com` wins over it.
        """
        host = (host or "").lower().strip(".")
        if not host:
            return set()
        labels = host.split(".")
        for start in range(len(labels)):
            candidate = ".".join(labels[start:])
            found = self.domains.get(candidate)
            if found is not None:
                return set(found)
        return set()

    def blocked_categories_of(self, host: str, blocked: list[str] | set[str]) -> set[str]:
        """Which of `blocked` this host falls under (empty means allow)."""
        return self.categories_of(host) & set(blocked)


def parse(doc: dict) -> Bundle:
    """Build a bundle from the on-disk JSON form."""
    if not isinstance(doc, dict):
        raise CategoryError("category bundle must be an object")
    domains: dict[str, tuple[str, ...]] = {}
    for category, entries in (doc.get("domains") or {}).items():
        if not isinstance(entries, list):
            raise CategoryError(f"category {category!r} must hold a list")
        for entry in entries:
            name = str(entry).lower().strip(". ")
            if not name:
                continue
            existing = domains.get(name, ())
            if category not in existing:
                domains[name] = (*existing, category)
    return Bundle(version=str(doc.get("version", "0")),
                  source=str(doc.get("source", "")), domains=domains)


def load(*directories: Path) -> Bundle:
    """Load the newest bundle available.

    A bundle delivered at runtime (portal, or an admin update) sits in
    /var/lib and wins over the one baked into the image, so lists can
    improve without shipping a new OS.
    """
    searched = directories or (LOCAL_BUNDLE_DIR, BUNDLE_DIR)
    for directory in searched:
        path = Path(directory) / "categories.json"
        try:
            bundle = parse(json.loads(path.read_text()))
        except FileNotFoundError:
            continue
        except (ValueError, CategoryError) as e:
            log.error("ignoring unusable category bundle at %s: %s", path, e)
            continue
        log.info("loaded %d categorised domains (version %s) from %s",
                 len(bundle), bundle.version, path)
        return bundle
    log.warning("no category bundle found; category filtering is inactive")
    return Bundle()


class SqliteBundle:
    """A category database too large to hold in memory.

    The imported lists run to millions of domains; a dict of those costs
    hundreds of megabytes of RAM, which is exactly what the family machine
    does not have. SQLite keeps them on disk and answers a lookup in
    microseconds from page cache.

    Lookups walk the domain's suffixes — at most a handful of indexed
    queries per host — so `cdn.example.com` still matches an entry for
    `example.com`, and the longest match wins as it does in memory.
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        # ONE SQLite CONNECTION PER THREAD, kept in thread-local storage.
        #
        # The proxy answers requests on mitmproxy's worker threads, and a
        # single sqlite3.Connection shared across threads — even with
        # check_same_thread=False — does NOT serialise cursor use: two
        # threads calling execute().fetchall() on the same connection can
        # clobber each other's results, and the failure is SILENT — a query
        # returns an empty set instead of raising. That is exactly what bit
        # us: a filtered user's traffic reached the proxy, the category
        # lookup came back empty on a contended query, nothing matched, and
        # a site that should have been blocked loaded. It was a heisenbug —
        # any added statement changed the timing and "fixed" it. A
        # per-thread connection removes the sharing entirely; each is
        # read-only (query_only) against the same on-disk file.
        self._local = threading.local()
        # Metadata and the count are read once, on the constructing thread.
        db = self._connect()
        meta = dict(db.execute("SELECT key, value FROM meta").fetchall())
        self.version = meta.get("version", "0")
        self.source = meta.get("source", "")
        self._count = db.execute("SELECT count(*) FROM domains").fetchone()[0]

    def _connect(self) -> sqlite3.Connection:
        db = getattr(self._local, "db", None)
        if db is None:
            db = sqlite3.connect(self.path, check_same_thread=False)
            db.execute("PRAGMA query_only = ON")
            self._local.db = db
        return db

    def __len__(self) -> int:
        return self._count

    @property
    def categories(self) -> set[str]:
        return {row[0] for row in
                self._connect().execute("SELECT DISTINCT category FROM domains")}

    def categories_of(self, host: str) -> set[str]:
        host = (host or "").lower().strip(".")
        if not host:
            return set()
        db = self._connect()
        labels = host.split(".")
        # Longest suffix first, so a specific subdomain beats its parent.
        for start in range(len(labels) - 1):
            candidate = ".".join(labels[start:])
            rows = db.execute(
                "SELECT category FROM domains WHERE domain = ?",
                (candidate,)).fetchall()
            if rows:
                return {row[0] for row in rows}
        return set()

    def blocked_categories_of(self, host: str, blocked) -> set[str]:
        return self.categories_of(host) & set(blocked)


def load_any(*directories: Path):
    """Load the best available bundle: the database if built, else JSON.

    A runtime bundle (portal-delivered, or an admin update) beats the one
    baked into the image.
    """
    searched = directories or (LOCAL_BUNDLE_DIR, BUNDLE_DIR)
    for directory in searched:
        database = Path(directory) / "categories.sqlite"
        if database.exists():
            try:
                bundle = SqliteBundle(database)
                log.info("loaded %d categorised domains (version %s) from %s",
                         len(bundle), bundle.version, database)
                return bundle
            except sqlite3.Error as e:
                log.error("ignoring unusable category database %s: %s",
                          database, e)
    return load(*directories)
