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

from pathlib import Path

OVERRIDE_DIR = Path("/var/lib/kosher-lists")
SHIPPED_DIR = Path("/usr/share/kosher")


def paths(name: str) -> tuple[Path, ...]:
    """Where to look for `name`, best first."""
    return (OVERRIDE_DIR / name, SHIPPED_DIR / name)
