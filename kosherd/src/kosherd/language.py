"""Cleaning up bad language in pages, without breaking them.

Two settings beyond off: substitute a milder word, or block the page. The
substitution is what people actually want day to day — a page that reads
normally minus the language beats a page that refuses to load, and it does
not teach anyone to route around the filter.

The hard part is not the word list, it is not corrupting the page. Running
a regex over raw HTML rewrites script source, attribute values, class names
and URLs, which breaks sites in ways nobody can debug. So the caller hands
this module TEXT ONLY (the contents of text nodes) and puts it back where
it came from.
"""

from __future__ import annotations

import json
import re
from . import lists
from . import textfold
from pathlib import Path

# Admin- or portal-supplied first, then the shipped default. See lists.py
# for why the override does not live under /var/lib/kosher.
WORDLIST_PATHS = lists.paths("wordlist.json")

OFF = "off"
SUBSTITUTE = "substitute"
BLOCK = "block"
MODES = (OFF, SUBSTITUTE, BLOCK)

# How each letter can be disguised. Only well-known substitutions: a list
# that treats every digit as a letter starts matching ordinary words.
LETTER_ALIASES = {
    "a": "a4@", "b": "b8", "c": "c(", "e": "e3", "g": "g6",
    "i": "i1!|", "l": "l1|", "o": "o0", "s": "s5$", "t": "t7+",
}
# Hebrew letters that take a different shape at the end of a word. A
# disguised or misspelt curse may use either, so a class holds both.
HEBREW_FINALS = {"כ": "כך", "ך": "כך", "מ": "מם", "ם": "מם", "נ": "נן",
                 "ן": "נן", "פ": "פף", "ף": "פף", "צ": "צץ", "ץ": "צץ"}
# A censor writes f*ck meaning the letter is there but hidden, so a
# wildcard has to stand FOR a letter rather than be deleted — an earlier
# version folded it away and then f*ck no longer matched fuck at all.
WILDCARDS = "*#"
# Padding between letters (f.u.c.k, f-u-c-k). Bounded, so a match cannot
# run across a whole sentence. A vowel point or an accent between letters
# is not padding but is not a letter either: a pointed זוֹנָה is the same
# word as זונה, so the marks are let through wherever padding is.
SEPARATOR = rf"{textfold.MARKS_CLASS}*[\s._\-]{{0,2}}"


def _letter_class(letter: str) -> str:
    """Everything one letter of a listed word may look like on a page.

    A plain Latin letter also matches its disguises (4 for a, $ for s)
    and its accented spellings (à á â for a), so one entry covers
    "cabrón", "cabron" and "c@bron". A letter with an accent that spells
    a different letter — ñ, ö, ő — stands only for itself; see textfold
    for which is which. ß is spelt ss as often as not, so it matches
    either.
    """
    letter = letter.lower()
    base = textfold.base_letter(letter)
    if base == "ss":
        return f"(?:ß|s{SEPARATOR}s)"
    if len(base) > 1:       # a ligature spelt as two letters
        return SEPARATOR.join(_letter_class(ch) for ch in base)
    if "a" <= base <= "z":
        chars = LETTER_ALIASES.get(base, base) + textfold.latin_variants(base)
    elif letter in HEBREW_FINALS:
        chars = HEBREW_FINALS[letter]
    else:
        # Cyrillic ё/е, an Arabic alef variant: base is the plain form,
        # and the class holds the plain form and this spelling of it.
        chars = "".join(dict.fromkeys(base + letter))
    return "[" + re.escape(chars + WILDCARDS) + "]"


def word_pattern(word: str) -> str:
    """A regex matching a word and its usual disguises."""
    return SEPARATOR.join(_letter_class(ch) for ch in word.lower()
                          if not textfold._MARKS_RE.match(ch))


class Wordlist:
    """Words to replace, and what to replace them with."""

    def __init__(self, replacements: dict[str, str] | None = None):
        self.replacements = {k.lower(): v for k, v in (replacements or {}).items()}
        self._patterns = self._compile()

    def _compile(self) -> list[tuple[re.Pattern | None, re.Pattern]]:
        """One pattern per script the list is written in.

        Matching costs time in proportion to the alternation, so a page is
        only scanned with the lists whose script it actually contains: the
        Hebrew words are never run over an English page, and vice versa.
        The Latin pattern always runs, because nearly every page has some
        Latin on it (a URL, a brand name) and the test would save nothing.

        Each pattern has ONE group, not one per word. A named group per
        word made the engine track 145 sets of boundaries through every
        character of every page, which measured 40 ms on a 28 KB page
        against 4 ms for the identical pattern without them — a tenfold
        cost for information that is cheap to recover afterwards (see
        _matched_word), and it was paid on every page whether or not
        anything matched.
        """
        if not self.replacements:
            self._each = []
            return []
        # Longest first, so "bullshit" wins over "shit".
        words = sorted(self.replacements, key=len, reverse=True)
        self._order = words
        self._each = [(w, re.compile(rf"{word_pattern(w)}\Z", re.IGNORECASE))
                      for w in words]
        by_script: dict[str, list[str]] = {}
        for word in words:
            by_script.setdefault(textfold.script_of(word), []).append(word)
        patterns = []
        for script, listed in by_script.items():
            alternation = "|".join(word_pattern(w) for w in listed)
            # Not \b: the disguised forms end in punctuation, which would
            # put a boundary in the wrong place. Require a non-letter
            # either side — in any script, not only [A-Za-z0-9] — and for
            # Hebrew and Arabic allow the prefixes those languages glue
            # onto a word, so זונה is found inside והזונה. A mark left
            # hanging after the last letter goes with the word.
            pattern = re.compile(
                rf"{textfold.word_start(script)}({alternation})"
                rf"{textfold.MARKS_CLASS}*{textfold.BOUNDARY_AFTER}",
                re.IGNORECASE)
            patterns.append((textfold.presence(script), pattern))
        return patterns

    def _applicable(self, text: str):
        for presence, pattern in self._patterns:
            if presence is None or presence.search(text):
                yield pattern

    def __len__(self) -> int:
        return len(self.replacements)

    def _matched_word(self, match: re.Match) -> str | None:
        """Which listed word this hit was.

        Worked out after the fact by testing the matched text — a few
        characters — against each word. That costs something only when
        there IS a match, which is rare, instead of costing something on
        every character of every page.
        """
        found = match.group(1)
        for word, pattern in self._each:
            if pattern.match(found):
                return word
        return None

    @staticmethod
    def _match_case(original: str, replacement: str) -> str:
        letters = [c for c in original if c.isalpha()]
        if letters and all(c.isupper() for c in letters) and len(letters) > 1:
            return replacement.upper()
        if letters and letters[0].isupper():
            return replacement.capitalize()
        return replacement

    def clean(self, text: str) -> tuple[str, int]:
        """Return (cleaned text, number of substitutions)."""
        if not self._patterns or not text:
            return text, 0
        count = 0

        def swap(match: re.Match) -> str:
            nonlocal count
            word = self._matched_word(match)
            if word is None:
                return match.group(0)
            count += 1
            return self._match_case(match.group(0), self.replacements[word])

        for pattern in self._applicable(text):
            text = pattern.sub(swap, text)
        return text, count

    def contains_any(self, text: str) -> bool:
        """Whether the text holds a listed word (for block mode)."""
        if not self._patterns or not text:
            return False
        return any(pattern.search(text) for pattern in self._applicable(text))


def _apply_delta(shipped: dict, add, remove) -> dict:
    words = dict(shipped.get("replacements", {}))
    words.update(add or {})
    for word in remove:
        words.pop(word.lower(), None)
        words.pop(word, None)
    return {"replacements": words}


def load(*paths: Path) -> Wordlist:
    """The word list in force: what ships, plus a family's edits on top."""
    if paths:
        # An explicit path is a test or a one-off, not the live lookup.
        for path in paths:
            try:
                doc = json.loads(Path(path).read_text())
            except (OSError, ValueError):
                continue
            return Wordlist(doc.get("replacements", {}))
        return Wordlist()
    doc = lists.resolve("wordlist.json", _apply_delta)
    return Wordlist(doc.get("replacements", {}))


# Everything between a tag's < and >, plus the contents of script and style,
# must survive untouched: rewriting inside them corrupts source, attribute
# values, class names and URLs, and breaks sites in ways nobody can debug.
_MARKUP = re.compile(
    r"(<script\b[^>]*>.*?</script\s*>"      # script contents
    r"|<style\b[^>]*>.*?</style\s*>"        # style contents
    r"|<!--.*?-->"                          # comments
    r"|<[^>]*>)",                           # any tag, with its attributes
    re.IGNORECASE | re.DOTALL,
)


def clean_html(html: str, wordlist: "Wordlist") -> tuple[str, int]:
    """Clean the visible text of an HTML document, leaving markup alone.

    Returns (html, substitutions). Splitting on markup and rewriting only
    the gaps keeps every tag, attribute and script byte-identical, which is
    what makes this safe to do to a live page.
    """
    if not len(wordlist) or not html:
        return html, 0
    total = 0
    out = []
    for piece in _MARKUP.split(html):
        if piece.startswith("<"):
            out.append(piece)          # markup: untouched
            continue
        cleaned, count = wordlist.clean(piece)
        total += count
        out.append(cleaned)
    return "".join(out), total


def html_contains_any(html: str, wordlist: "Wordlist") -> bool:
    """Whether the visible text holds a listed word (for block mode)."""
    if not len(wordlist) or not html:
        return False
    return any(not piece.startswith("<") and wordlist.contains_any(piece)
               for piece in _MARKUP.split(html))
