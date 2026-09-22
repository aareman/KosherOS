#!/usr/bin/env python3
"""Build the shipped word lists from one source file per language.

The filter ships three lists: the bad language it cleans off a page
(wordlist.json), the words a page is judged by (content-terms.json) and
the searches it refuses to run (search-blocklist.json). Each is one flat
file, because that is what the daemon, the proxy, the search service,
the portal and the admin app all read, and none of them care what
language a word is in.

A maintainer does. Fifteen languages in one flat file is unreviewable:
somebody fixing the Russian list should not scroll past the Hebrew one
to find it. So the SOURCE is os-image/lists/<language>.json, one file per
language holding that language's share of all three lists, and this
script merges them into the shipped files. A test fails when the shipped
files are out of date, so the two cannot drift apart.

    python3 scripts/build-lists.py            # rebuild the shipped files
    python3 scripts/build-lists.py --check    # exit 1 if they are stale

Every list is matched in one alternation per script (see textfold), so
words from different Latin-script languages share one matcher and one
replacement: "puta" is Spanish and Portuguese, and whichever language is
merged first supplies the replacement both will see. The merge order is
English, then the others by code, and a word that two languages spell
identically is reported so the replacement can be chosen to read in both.
Two DIFFERENT words that fold to the same spelling (Turkish "piç" and
English "pic") are a mistake — the pattern for one would match the other
on every page — and the build refuses them.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCES = ROOT / "os-image" / "lists"
SHIPPED = ROOT / "os-image" / "files" / "usr" / "share" / "kosher"

sys.path.insert(0, str(ROOT / "kosherd" / "src"))
from kosherd import textfold  # noqa: E402

WORDLIST = "wordlist.json"
CONTENT = "content-terms.json"
SEARCHES = "search-blocklist.json"
FIRST = "en"


def key(term: str) -> str:
    """The spelling two terms are compared under: folded, lowercase, one
    space between words."""
    return " ".join(textfold.fold(term).lower().split())


def language_files() -> list[Path]:
    files = sorted(p for p in SOURCES.glob("*.json") if p.name != "headers.json")
    files.sort(key=lambda p: (p.stem != FIRST, p.stem))
    return files


class Merge:
    def __init__(self) -> None:
        self.notes: list[str] = []
        self.errors: list[str] = []
        self.languages: list[str] = []
        self._owner: dict[str, tuple[str, str]] = {}   # key -> (word, language)
        self.replacements: dict[str, str] = {}
        self.searches: list[str] = []
        self.content: dict[str, dict[str, list[str]]] = {}
        self._content_seen: dict[str, tuple[str, str, str, str]] = {}

    def _claim(self, term: str, lang: str, what: str) -> bool:
        """Whether this term is new to its list. A term two languages
        spell identically is shared; two different terms that fold
        together are refused."""
        k = key(term)
        if not k:
            self.errors.append(f"{lang}: an empty {what}")
            return False
        owner = self._owner.get((what, k))
        if owner is None:
            self._owner[(what, k)] = (term, lang)
            return True
        word, first = owner
        if word.lower() == term.lower():
            self.notes.append(f"{what} {term!r} is shared by {first} and {lang}; {first}'s entry is used")
        else:
            # The same spelling once the accents are folded: usually one
            # word written with and without them (pornografía and
            # pornografia), which the matcher already treats as one. It
            # can also be two different words (Turkish piç and English
            # pic), which no rule can tell apart from here — the sweep
            # over ordinary text in the tests is what catches those.
            self.notes.append(f"{what} {term!r} ({lang}) folds to {first}'s {word!r}; one entry is kept")
        return False

    def add(self, doc: dict, lang: str) -> None:
        self.languages.append(lang)
        for word, replacement in (doc.get("replacements") or {}).items():
            if not isinstance(replacement, str) or not replacement.strip():
                self.errors.append(f"{lang}: {word!r} has no replacement")
                continue
            if self._claim(word, lang, "word"):
                self.replacements[word.lower()] = replacement
        for term in doc.get("searches") or []:
            if self._claim(term, lang, "search term"):
                self.searches.append(term.lower())
        for level, by_weight in (doc.get("content") or {}).items():
            for weight, terms in by_weight.items():
                for term in terms:
                    if self._claim(term, lang, "content term"):
                        self.content.setdefault(level, {}).setdefault(str(weight), []).append(term.lower())

    def documents(self, headers: dict) -> dict[str, dict]:
        def doc(name: str, body: dict) -> dict:
            head = dict(headers.get(name, {}))
            head["languages"] = list(self.languages)
            head.update(body)
            return head

        # Weights in descending order within each level, levels in the
        # order the scorer names them, so the file reads top down.
        content = {}
        for level in ("nsfw", "suggestive", "immodest"):
            if level in self.content:
                content[level] = {w: self.content[level][w] for w in
                                  sorted(self.content[level], key=lambda x: -int(x))}
        return {
            WORDLIST: doc(WORDLIST, {"replacements": self.replacements}),
            CONTENT: doc(CONTENT, {"terms": content}),
            SEARCHES: doc(SEARCHES, {"terms": self.searches}),
        }


def render(doc: dict) -> str:
    return json.dumps(doc, indent=2, ensure_ascii=False) + "\n"


def build() -> tuple[dict[str, str], Merge]:
    headers = json.loads((SOURCES / "headers.json").read_text())
    merge = Merge()
    for path in language_files():
        doc = json.loads(path.read_text())
        merge.add(doc, doc.get("language") or path.stem)
    rendered = {name: render(d) for name, d in merge.documents(headers).items()}
    return rendered, merge


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--check", action="store_true",
                        help="report stale shipped files instead of writing them")
    parser.add_argument("--verbose", action="store_true",
                        help="list the terms shared between languages")
    args = parser.parse_args()

    rendered, merge = build()
    if merge.errors:
        for error in merge.errors:
            print(f"error: {error}", file=sys.stderr)
        return 2
    if args.verbose:
        for note in merge.notes:
            print(f"note: {note}")

    stale = [name for name, text in rendered.items()
             if not (SHIPPED / name).exists() or (SHIPPED / name).read_text() != text]
    if args.check:
        if stale:
            print("shipped lists are out of date: " + ", ".join(stale), file=sys.stderr)
            print("run: python3 scripts/build-lists.py", file=sys.stderr)
            return 1
        print(f"shipped lists are up to date ({', '.join(merge.languages)})")
        return 0
    for name in stale:
        (SHIPPED / name).write_text(rendered[name])
        print(f"wrote {SHIPPED / name}")
    if not stale:
        print("shipped lists already up to date")
    return 0


if __name__ == "__main__":
    sys.exit(main())
