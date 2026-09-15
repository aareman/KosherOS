"""Ready-made approved-site lists for Whitelist only accounts.

A whitelist account reaches the sites on its list and nothing else, which is
the strongest thing this product offers and also the most work: typing the
thirty hosts that make Sefaria or Gmail actually load is not something a
parent should ever be asked to do. So the image ships bundles — "Torah
study", "Email and files" — and an account can have any combination of them
switched on, merged with whatever the family adds itself.

Each bundle names the sites AND what those sites load from. A site whose
fonts and scripts are blocked looks broken rather than blocked, and a
family that sees broken pages turns the filter off.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

log = logging.getLogger(__name__)

BUNDLE_PATH = Path("/usr/share/kosher/whitelist-bundles.json")
# An admin- or portal-supplied replacement, alongside the other list
# overrides, so a family or a portal can ship its own bundles without a new
# OS image.
OVERRIDE_PATH = Path("/var/lib/kosher-lists/whitelist-bundles.json")

_cache: tuple[float, dict] | None = None


def _read(path: Path) -> dict | None:
    try:
        document = json.loads(path.read_text())
    except (OSError, ValueError) as e:
        if path.exists():
            log.warning("ignoring unreadable whitelist bundles at %s: %s", path, e)
        return None
    if not isinstance(document, dict) or not isinstance(document.get("bundles"), list):
        log.warning("whitelist bundles at %s are not the expected shape", path)
        return None
    return document


def load() -> dict:
    """The bundle document, from the override if there is one."""
    global _cache

    for path in (OVERRIDE_PATH, BUNDLE_PATH):
        if not path.exists():
            continue
        mtime = path.stat().st_mtime
        if _cache is not None and _cache[0] == mtime:
            return _cache[1]
        document = _read(path)
        if document is not None:
            _cache = (mtime, document)
            return document
    return {"bundles": [], "shared": {"domains": []}}


def describe() -> list[dict]:
    """The bundles as the admin app offers them: key, label, description, size."""
    document = load()
    found = []
    for bundle in document.get("bundles", []):
        key = str(bundle.get("key") or "")
        if not key:
            continue
        found.append({
            "key": key,
            "label": str(bundle.get("label") or key),
            "description": str(bundle.get("description") or ""),
            "domains": len(bundle.get("domains") or []),
        })
    return found


def keys() -> set[str]:
    return {b["key"] for b in describe()}


def domains(chosen) -> list[str]:
    """Every domain the chosen bundles bring, the shared hosts included.

    The shared hosts (fonts, script CDNs) ride along as soon as ANY bundle
    is on, because every bundle's sites need them; with none chosen, this
    is empty and the account is limited to whatever the family listed.
    """
    wanted = {str(k) for k in (chosen or ())}
    if not wanted:
        return []
    document = load()
    found: list[str] = []
    for bundle in document.get("bundles", []):
        if str(bundle.get("key")) in wanted:
            found += [str(d).strip().lower() for d in bundle.get("domains") or []]
    found += [str(d).strip().lower()
              for d in (document.get("shared") or {}).get("domains") or []]
    return sorted({d for d in found if d})


def effective(user) -> list[str]:
    """The whole approved list for an account: its bundles plus its own."""
    own = _field(user, "whitelist", []) or []
    chosen = _field(user, "whitelist_bundles", []) or []
    return sorted({*(str(d).strip().lower() for d in own), *domains(chosen)})


def _field(user, name, default=None):
    if isinstance(user, dict):
        return user.get(name, default)
    return getattr(user, name, default)
