"""Where an admin- or portal-supplied list lives.

Every list the filter uses ships complete and can be replaced or extended
by an admin or by the portal. That override used to be written under
/var/lib/kosher, which is mode 0700 because it holds the policy — so the
two processes that do the actual filtering, both unprivileged, could not
read a single one of them. They fell back to the shipped copy and said
nothing, which meant the documented behaviour simply did not happen.

So overrides live here instead: a directory of nothing but lists, world
readable, with no secrets in it. kosherd writes it; the proxy and the
search service read it.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

log = logging.getLogger(__name__)

# Three layers, in the order they win.
#
#   shipped   what came with the image
#   portal    a signed update, so a list can improve without an OS rebuild
#   edits     this family's handful of additions and removals
#
# The portal layer exists because a filter that can only improve when the
# operating system is rebuilt is a filter that goes stale. It replaces the
# shipped copy rather than merging with it — upstream owns that list — and
# the family's delta is applied on top of whichever base is in force, so a
# portal update never silently discards a family's edits.
SHIPPED_DIR = Path("/usr/share/kosher")
OVERRIDE_DIR = Path("/var/lib/kosher-lists")
PORTAL_DIR = OVERRIDE_DIR / "portal"
PORTAL_VERSION_PATH = PORTAL_DIR / "version.json"


def paths(name: str) -> tuple[Path, ...]:
    """Where to look for `name`, best first."""
    return (OVERRIDE_DIR / name, PORTAL_DIR / name, SHIPPED_DIR / name)


def base_document(name: str) -> dict:
    """The list before this family's edits: the portal's copy, or shipped."""
    for path in (PORTAL_DIR / name, SHIPPED_DIR / name):
        try:
            return json.loads(path.read_text())
        except (OSError, ValueError):
            continue
    return {}


def portal_version() -> int:
    try:
        return int(json.loads(PORTAL_VERSION_PATH.read_text())["version"])
    except (OSError, ValueError, KeyError, TypeError):
        return 0


def install_portal_lists(documents: dict, version: int) -> list[str]:
    """Write a verified set of portal lists. Returns the names written.

    Called only with a document whose signature has already been checked
    against the enrolled portal key (see sync.verify_envelope).
    """
    PORTAL_DIR.mkdir(parents=True, exist_ok=True)
    written = []
    for name, document in documents.items():
        if name not in ALLOWED_LISTS:
            log.warning("portal sent an unknown list %r; ignoring", name)
            continue
        if not isinstance(document, dict):
            log.warning("portal sent a malformed %s; ignoring", name)
            continue
        target = PORTAL_DIR / name
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_text(json.dumps(document) + "\n")
        tmp.chmod(0o644)
        tmp.replace(target)
        written.append(name)
    PORTAL_VERSION_PATH.write_text(
        json.dumps({"version": int(version), "lists": sorted(written)}) + "\n")
    PORTAL_VERSION_PATH.chmod(0o644)
    return written


# Only these, by name. A portal that could write any filename into a
# directory the filter reads could replace something that is not a list.
ALLOWED_LISTS = frozenset({
    "wordlist.json", "content-terms.json", "site-rules.json",
    "search-blocklist.json",
})


# A family's edits are a DELTA, not a replacement.
#
# The lists ship complete on purpose: an ordinary family should never have
# to build one. What they will do is disagree with a handful of entries —
# a word they want cleaned that is not listed, a term that keeps blocking
# something innocent. If that edit were a copy of the whole list, the
# family would silently stop receiving every later improvement to it, and
# nobody would notice for a year.
#
# So an override may be `{"add": ..., "remove": [...]}`, which is applied
# on top of whatever ships. A whole-document override is still honoured,
# because the portal may legitimately want to ship a complete replacement.
ADD = "add"
REMOVE = "remove"


def is_delta(doc) -> bool:
    return isinstance(doc, dict) and (ADD in doc or REMOVE in doc)


def read(name: str) -> tuple[dict, dict | None]:
    """The base document, and this family's edits sitting on top of it."""
    base = base_document(name)
    try:
        override = json.loads((OVERRIDE_DIR / name).read_text())
    except (OSError, ValueError):
        override = None
    return base, override


def resolve(name: str, apply_delta) -> dict:
    """The document the filter should actually use.

    `apply_delta(shipped, add, remove)` knows the shape of this particular
    list; everything else about overrides is the same for all of them.
    """
    shipped, override = read(name)
    if override is None:
        return shipped
    if not is_delta(override):
        return override  # a complete replacement, as the portal may send
    return apply_delta(shipped, override.get(ADD), override.get(REMOVE) or [])


def save_delta(name: str, add=None, remove=None) -> None:
    """Record a family's edits. Called by kosherd, which runs as root."""
    OVERRIDE_DIR.mkdir(parents=True, exist_ok=True)
    doc = {ADD: add or {}, REMOVE: sorted(remove or [])}
    target = OVERRIDE_DIR / name
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(json.dumps(doc, indent=2) + "\n")
    tmp.chmod(0o644)
    tmp.replace(target)
