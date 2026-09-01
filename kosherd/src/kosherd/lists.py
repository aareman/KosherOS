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
from pathlib import Path

OVERRIDE_DIR = Path("/var/lib/kosher-lists")
SHIPPED_DIR = Path("/usr/share/kosher")


def paths(name: str) -> tuple[Path, ...]:
    """Where to look for `name`, best first."""
    return (OVERRIDE_DIR / name, SHIPPED_DIR / name)


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
    """The shipped document, and the override sitting on top of it."""
    shipped, override = {}, None
    override_path, shipped_path = paths(name)
    for path, is_override in ((shipped_path, False), (override_path, True)):
        try:
            doc = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if is_override:
            override = doc
        else:
            shipped = doc
    return shipped, override


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
