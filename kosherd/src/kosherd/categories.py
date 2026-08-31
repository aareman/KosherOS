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
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

BUNDLE_DIR = Path("/usr/share/kosher/categories")
LOCAL_BUNDLE_DIR = Path("/var/lib/kosher/categories")

# What each category means, for the admin UI. The wording matters: an admin
# is choosing on behalf of a family, so the label has to say what will
# actually stop working.
CATEGORY_LABELS = {
    "adult": "Adult and pornography",
    "gambling": "Gambling and betting",
    "dating": "Dating and matchmaking",
    "social": "Social networks",
    "video": "Video and streaming",
    "shopping": "Shopping and marketplaces",
    "games": "Games",
    "news": "News and media",
    "ads": "Advertising and trackers",
    "malware": "Malware and phishing",
    "proxy": "Proxies, VPNs and filter bypass",
}

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
