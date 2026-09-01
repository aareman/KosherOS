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
from pathlib import Path

WORDLIST_PATHS = (
    Path("/var/lib/kosher/wordlist.json"),   # admin- or portal-supplied
    Path("/usr/share/kosher/wordlist.json"),  # shipped default
)

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
# A censor writes f*ck meaning the letter is there but hidden, so a
# wildcard has to stand FOR a letter rather than be deleted — an earlier
# version folded it away and then f*ck no longer matched fuck at all.
WILDCARDS = "*#"
# Padding between letters (f.u.c.k, f-u-c-k). Bounded, so a match cannot
# run across a whole sentence.
SEPARATOR = r"[\s._\-]{0,2}"

OFF = "off"
SUBSTITUTE = "substitute"
BLOCK = "block"
MODES = (OFF, SUBSTITUTE, BLOCK)


def _letter_class(letter: str) -> str:
    aliases = LETTER_ALIASES.get(letter, letter)
    return "[" + re.escape(aliases + WILDCARDS) + "]"


def word_pattern(word: str) -> str:
    """A regex matching a word and its usual disguises."""
    return SEPARATOR.join(_letter_class(ch) for ch in word.lower())


class Wordlist:
    """Words to replace, and what to replace them with."""

    def __init__(self, replacements: dict[str, str] | None = None):
        self.replacements = {k.lower(): v for k, v in (replacements or {}).items()}
        self._pattern = self._compile()

    def _compile(self) -> re.Pattern | None:
        if not self.replacements:
            return None
        # Longest first, so "bullshit" wins over "shit".
        words = sorted(self.replacements, key=len, reverse=True)
        self._order = words
        # ONE group, not one per word. A named group per word made the
        # engine track 145 sets of boundaries through every character of
        # every page, which measured 40 ms on a 28 KB page against 4 ms
        # for the identical pattern without them — a tenfold cost for
        # information that is cheap to recover afterwards (see
        # _matched_word), and it was paid on every page whether or not
        # anything matched.
        self._each = [(w, re.compile(rf"{word_pattern(w)}\Z", re.IGNORECASE))
                      for w in words]
        alternation = "|".join(word_pattern(w) for w in words)
        # Not \b: the disguised forms end in punctuation, which would put a
        # boundary in the wrong place. Require a non-letter either side.
        return re.compile(rf"(?<![A-Za-z0-9])({alternation})(?![A-Za-z0-9])",
                          re.IGNORECASE)

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
        if not self._pattern or not text:
            return text, 0
        count = 0

        def swap(match: re.Match) -> str:
            nonlocal count
            word = self._matched_word(match)
            if word is None:
                return match.group(0)
            count += 1
            return self._match_case(match.group(0), self.replacements[word])

        return self._pattern.sub(swap, text), count

    def contains_any(self, text: str) -> bool:
        """Whether the text holds a listed word (for block mode)."""
        return bool(self._pattern and text and self._pattern.search(text))


def load(*paths: Path) -> Wordlist:
    """Load the first available word list; an admin copy beats the shipped one."""
    for path in (paths or WORDLIST_PATHS):
        try:
            doc = json.loads(Path(path).read_text())
        except (OSError, ValueError):
            continue
        return Wordlist(doc.get("replacements", {}))
    return Wordlist()


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
