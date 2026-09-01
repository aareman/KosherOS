"""Is the filter actually holding the lists it thinks it has?

Every list loader in this codebase fails open and quiet. Miss the category
database and `load_any` returns an empty bundle; miss the content terms and
the scorer matches nothing; miss the word list and the profanity filter is
off. Each of those is the right *behaviour* — a missing file must not take
the machine down — and each is silent, which is the part that is wrong.
The account still says "Filtered internet" and nothing is being filtered.

So the datasets get counted and reported, next to the services and the
picture state, and an empty one is stated as a fault in words rather than
left for somebody to deduce from the web being suspiciously usable.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)

# Below these a list is not merely small, it is broken: the shipped lists
# are far larger, so anything under them means a truncated or empty file.
MINIMUM = {
    "categories": 1000,
    "content-terms": 50,
    "wordlist": 20,
    "site-rules": 1,
    "search-blocklist": 20,
}

# What to call each of these to a person. "the wordlist list" is what you
# get for free, and it is not a sentence anyone should have to read.
LABELS = {
    "categories": "list of classified sites",
    "content-terms": "list of words pages are judged by",
    "wordlist": "bad-language list",
    "site-rules": "shop department rules",
    "search-blocklist": "blocked search terms",
}


def _count(name: str) -> tuple[int, str | None]:
    """How many entries `name` holds, and where it came from."""
    from . import categories, content, language, lists, search, siterules

    if name == "categories":
        bundle = categories.load_any()
        source = getattr(bundle, "path", None)
        return len(bundle), str(source) if source else None
    loaders = {
        "content-terms": lambda: len(content.load().terms),
        "wordlist": lambda: len(language.load()),
        "site-rules": lambda: len(siterules.load()),
        "search-blocklist": lambda: len(search.load_blocklist()),
    }
    count = loaders[name]()
    for candidate in lists.paths(f"{name}.json"):
        if candidate.exists():
            return count, str(candidate)
    return count, None


def datasets() -> list[dict]:
    """Every list the filter depends on, with whether it actually loaded."""
    found = []
    for name, floor in MINIMUM.items():
        try:
            count, source = _count(name)
        except Exception:  # noqa: BLE001 - a check must not be the thing that breaks
            log.exception("could not check the %s list", name)
            count, source = 0, None
        found.append({
            "name": name,
            "label": LABELS.get(name, name),
            "entries": count,
            "source": source,
            "ok": count >= floor,
        })
    return found


def problems(found: list[dict] | None = None) -> list[str]:
    """What to tell an admin, in words they can act on."""
    said = []
    for entry in found if found is not None else datasets():
        if entry["ok"]:
            continue
        label = LABELS.get(entry["name"], entry["name"])
        if entry["entries"] == 0:
            said.append(f"the {label} did not load, so nothing it covers "
                        "is being filtered")
        else:
            said.append(f"the {label} holds only {entry['entries']} "
                        "entries, so it is truncated or damaged")
    return said
