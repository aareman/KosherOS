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
# word as זונה, so the marks are let through wherever padding is. The
# marks class depends on the word's script; this is the Latin one.
PADDING = r"[\s._\-]{0,2}"
SEPARATOR = rf"{textfold.marks_class(textfold.LATIN)}*{PADDING}"

# Below this many letters a word is matched as written, with no disguises
# and no padding. A three-letter word has too many disguises that are
# something else: "1 ul" in a stylesheet is l-u-l, "8.5" is b-s, "455" is
# a-s-s. Nobody disguises a short word anyway; the disguises are for the
# words a censor would catch.
MIN_DISGUISED = 4


def _letter_class(letter: str, sep: str, disguised: bool) -> str:
    """Everything one letter of a listed word may look like on a page.

    A plain Latin letter also matches its accented spellings (à á â for
    a) and, in a word long enough to be disguised, its disguises (4 for
    a, $ for s), so one entry covers "cabrón", "cabron" and "c@bron". A
    letter with an accent that spells a different letter — ñ, ö, ő —
    stands only for itself; see textfold for which is which. ß is spelt
    ss as often as not, so it matches either.
    """
    letter = letter.lower()
    base = textfold.base_letter(letter)
    if len(base) > 1:
        # A letter spelt as two (ß as ss, the Yiddish ײ as יי): itself,
        # or the two letters it stands for.
        spelt_out = sep.join(_letter_class(ch, sep, disguised) for ch in base)
        return f"(?:{re.escape(letter)}|{spelt_out})"
    if "a" <= base <= "z":
        chars = (LETTER_ALIASES.get(base, base) if disguised else base) + textfold.variants(base)
    elif letter in HEBREW_FINALS:
        chars = HEBREW_FINALS[letter]
    else:
        # Cyrillic е and ё, an alef and its variants, the Arabic and
        # Persian kaf: the plain form and every other spelling of it.
        chars = "".join(dict.fromkeys(base + letter + textfold.variants(base)))
    return "[" + re.escape(chars + (WILDCARDS if disguised else "")) + "]"


def _parts(word: str) -> tuple[str, str]:
    """A word's pattern in two pieces: its first letter, and the rest.

    Split so that an alternation of many words can be factored by first
    letter (see Wordlist._compile). Joined, the two are the whole word.
    """
    marks = textfold.marks_class(textfold.script_of(word))
    letters = [ch for ch in word.lower() if not textfold._MARKS_RE.match(ch)]
    disguised = sum(ch.isalpha() for ch in letters) >= MIN_DISGUISED
    # Marks between the letters are always let through, since a pointed
    # word is the same word; padding only where a disguise is allowed.
    sep = rf"{marks}*{PADDING}" if disguised else f"{marks}*"
    classes = [_letter_class(ch, sep, disguised) if ch.isalpha() else re.escape(ch)
               for ch in letters]
    if not classes:
        return "", ""
    return classes[0], "".join(sep + c for c in classes[1:])


def word_pattern(word: str) -> str:
    """A regex matching a word and its usual disguises."""
    head, tail = _parts(word)
    return head + tail


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
        self._heads: list[tuple[re.Pattern, list[str]]] = []
        self._each: dict[str, re.Pattern] = {}
        if not self.replacements:
            return []
        # Longest first, so "bullshit" wins over "shit".
        words = sorted(self.replacements, key=len, reverse=True)
        self._order = words
        by_script: dict[str, list[str]] = {}
        for word in words:
            by_script.setdefault(textfold.script_of(word), []).append(word)
        patterns = []
        for script, listed in by_script.items():
            # Factored by first letter: a thousand alternatives tried at
            # every position of every page is what made a page cost 60 ms;
            # one character class per first letter, and the thirty or so
            # words behind it only when it matches, brings that back to a
            # few. The order within a group stays longest first.
            groups: dict[str, tuple[list[str], list[str]]] = {}
            for word in listed:
                head, tail = _parts(word)
                if head:
                    tails, members = groups.setdefault(head, ([], []))
                    tails.append(tail)
                    members.append(word)
            alternation = "|".join(f"{head}(?:{'|'.join(tails)})"
                                   for head, (tails, _members) in groups.items())
            # Not \b: the disguised forms end in punctuation, which would
            # put a boundary in the wrong place. Require a non-letter
            # either side — in any script, not only [A-Za-z0-9] — and for
            # Hebrew and Arabic allow the prefixes those languages glue
            # onto a word, so זונה is found inside והזונה. A mark left
            # hanging after the last letter goes with the word.
            pattern = re.compile(
                rf"{textfold.word_start(script)}({alternation})"
                rf"{textfold.marks_class(script)}*{textfold.BOUNDARY_AFTER}",
                re.IGNORECASE)
            patterns.append((textfold.presence(script), pattern))
            # For naming the word that hit: the same groups, so only the
            # words that share the hit's first letter are ever tried.
            for head, (_tails, members) in groups.items():
                self._heads.append((re.compile(head, re.IGNORECASE), members))
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
        for head, members in self._heads:
            if not head.match(found):
                continue
            for word in members:
                pattern = self._each.get(word)
                if pattern is None:
                    # Compiled the first time a word is a candidate, not
                    # for every word at load: fifteen hundred small
                    # patterns cost two seconds up front, for hits that
                    # are rare.
                    pattern = self._each[word] = re.compile(
                        rf"{word_pattern(word)}\Z", re.IGNORECASE)
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
            if word is None or not plausible(word, match.group(0)):
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
        for pattern in self._applicable(text):
            for match in pattern.finditer(text):
                word = self._matched_word(match)
                if word is not None and plausible(word, match.group(0)):
                    return True
        return False


def plausible(word: str, found: str) -> bool:
    """Is what matched a disguised spelling of the word, or a number?

    A disguise stands a digit or a symbol in for a letter or two; it does
    not spell the whole word in them. So at least half of the word's
    letters must be there as letters: sh1t and f*ck pass, 8008 is not
    "boob", and 63c1 in a run of SVG path data is not "geci".
    """
    letters = sum(ch.isalpha() for ch in word)
    return sum(ch.isalpha() for ch in found) * 2 >= letters


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
