"""What counts as the same word, across the scripts a family reads.

The filter's word lists were English and its matchers assumed it: a word
boundary was "not [A-Za-z0-9]", a disguised letter was a Latin letter, and
a page in Hebrew, Russian, French or Arabic was, as far as the matchers
were concerned, one long word. This module holds the few facts about
writing systems that every matcher needs, so that the bad-language list,
the page scorer, the search blocklist and the shop rules all agree on
them.

Four things, and no more:

* Which SCRIPT a word is written in, so a Hebrew list is only run over a
  page that has Hebrew on it. Matching costs time in proportion to the
  list, and a family's Russian list should not slow down every English
  page they read.

* Which MARKS may sit between letters without changing the word: Hebrew
  points and cantillation, Arabic vowel signs, and the combining accents
  a page uses when its text is not precomposed. A vocalised siddur and
  a plain one hold the same words.

* Which ACCENTED LETTERS are spellings of a plain one. "cabrón" and
  "cabron" are the same word, and so are "bâtard" and "batard", because
  people drop the accent when they type. But "ñ" is not "n" (coño is a
  curse, cono is a cone), "ö" is not "o" (Turkish göt is a curse, English
  got is not) and "ő" is not "o": only the accents that mark stress or
  vowel quality on the same letter are folded, never the ones that spell
  a different letter.

* Which PREFIXES Hebrew and Arabic glue onto the front of a word. Hebrew
  writes "and the bikini" as one word (והביקיני) and Arabic writes "the
  porn" as one (البورن), so a whole-word match has to allow for a
  prefix and nothing else.

`fold()` applies all of that to a text for the matchers that only need to
know WHETHER a word is there (the page scorer, the shop rules). The
bad-language filter rewrites text in place and cannot fold it, so it
builds the same tolerance into its patterns instead, using the tables
here.
"""

from __future__ import annotations

import re
import unicodedata

# -- scripts ---------------------------------------------------------------

LATIN = "latin"          # and anything else: digits, symbols, CJK
HEBREW = "hebrew"        # Hebrew and Yiddish
CYRILLIC = "cyrillic"    # Russian, Ukrainian and the rest
ARABIC = "arabic"        # Arabic and Persian

_RANGES = {
    HEBREW: "\u0590-\u05ff\ufb1d-\ufb4f",
    CYRILLIC: "\u0400-\u052f",
    ARABIC: "\u0600-\u06ff\u0750-\u077f\ufb50-\ufdff\ufe70-\ufeff",
}
_PRESENCE = {name: re.compile(f"[{rng}]") for name, rng in _RANGES.items()}
_SCRIPT_OF_CHAR = [(re.compile(f"^[{rng}]$"), name) for name, rng in _RANGES.items()]


def script_of(word: str) -> str:
    """The script a word is written in, judged by its first letter."""
    for ch in word:
        if not ch.isalpha():
            continue
        for pattern, name in _SCRIPT_OF_CHAR:
            if pattern.match(ch):
                return name
        return LATIN
    return LATIN


def presence(script: str) -> re.Pattern | None:
    """A cheap test for whether a text holds any of this script at all.

    None for Latin: nearly every page has some Latin on it (a URL, a brand
    name), so there is nothing to save by asking.
    """
    return _PRESENCE.get(script)


def scripts_in(text: str) -> set[str]:
    found = {name for name, pattern in _PRESENCE.items() if pattern.search(text)}
    found.add(LATIN)
    return found


# -- marks that may sit between letters -------------------------------------

# Combining accents; Hebrew points, cantillation and meteg; Arabic vowel
# signs, shadda, sukun and the Quranic annotation marks.
MARKS = "\u0300-\u036f\u0591-\u05bd\u05bf\u05c1\u05c2\u05c4\u05c5\u05c7" \
        "\u064b-\u065f\u0670\u06d6-\u06dc\u06df-\u06e4\u06e7\u06e8\u06ea-\u06ed"
MARKS_CLASS = f"[{MARKS}]"
_MARKS_RE = re.compile(MARKS_CLASS)

# The marks a word of one script can carry, for a pattern that only has
# to allow for those: a Latin word never has a Hebrew point in it, and
# the class is repeated between every two letters of every listed word.
_MARKS_BY_SCRIPT = {
    LATIN: "̀-ͯ",
    CYRILLIC: "̀-ͯ",
    HEBREW: "֑-ׇֽֿׁׂׅׄ",
    ARABIC: "ً-ٰٟۖ-ۜ۟-۪ۤۧۨ-ۭ",
}


def marks_class(script: str) -> str:
    return f"[{_MARKS_BY_SCRIPT.get(script, MARKS)}]"


# -- accented letters that spell a plain one ---------------------------------

# Acute, grave, circumflex, cedilla, and a tilde on a or o. Not the
# diaeresis (ä ö ü are their own letters in German, Turkish and
# Hungarian), not the double acute (ő ű), not the tilde on n (ñ), not the
# ring (å), not the caron (č š ž), not a stroke or a dotless i.
_FOLDED_MARKS = {"\u0301", "\u0300", "\u0302", "\u0327"}
_TILDE = "\u0303"


def _build_fold_table() -> dict[str, str]:
    table: dict[str, str] = {}
    for start, stop in ((0x00C0, 0x0250), (0x1E00, 0x1F00)):
        for code in range(start, stop):
            ch = chr(code)
            parts = unicodedata.normalize("NFD", ch)
            base, marks = parts[0], set(parts[1:])
            if not marks or not ("a" <= base.lower() <= "z"):
                continue
            allowed = set(_FOLDED_MARKS)
            if base.lower() in "ao":
                allowed.add(_TILDE)
            if marks <= allowed:
                table[ch] = base
    # Letters that decompose to nothing but are spelt two ways.
    table["ß"] = "ss"
    table["ẞ"] = "SS"
    # Russian writes ё as е more often than not.
    table["ё"] = "е"
    table["Ё"] = "Е"
    # Arabic: the alef variants, the final ya spelt as alef maksura, and
    # ta marbuta written as ha. Persian's own kaf and ya fold to the
    # Arabic ones so one list serves both.
    for variant in "أإآٱ":
        table[variant] = "ا"
    table["ى"] = "ي"
    table["ة"] = "ه"
    table["ک"] = "ك"
    table["ی"] = "ي"
    # Hebrew presentation forms (a shin with its dot precomposed, and
    # the like) come apart under NFKC; the Yiddish ligatures do not.
    table["ײ"] = "יי"
    table["ױ"] = "וי"
    table["װ"] = "וו"
    return table


FOLD_TABLE = _build_fold_table()

# For the character classes in a pattern: the spellings from Latin-1 and
# Latin Extended-A (the European languages) plus the non-Latin entries.
# The rest of the fold table — Vietnamese tone letters and the like — is
# folded when text is scored, but would triple the size of every class in
# a pattern that is repeated for every letter of every listed word.
_VARIANTS: dict[str, str] = {}
for _ch, _base in FOLD_TABLE.items():
    if len(_base) == 1 \
            and not (0x180 <= ord(_ch) <= 0x24F or 0x1E00 <= ord(_ch) <= 0x1EFF) \
            and _ch.lower() not in _VARIANTS.get(_base.lower(), ""):
        _VARIANTS[_base.lower()] = _VARIANTS.get(_base.lower(), "") + _ch.lower()


def base_letter(ch: str) -> str:
    """The plain letter this one is a spelling of, or itself."""
    return FOLD_TABLE.get(ch, ch)


def variants(base: str) -> str:
    """Every other lowercase spelling of a plain letter: à á â for a, ё for
    е, the alef variants for ا, the Persian kaf for ك."""
    return _VARIANTS.get(base.lower(), "")


def fold(text: str) -> str:
    """One spelling for a text, for matchers that only ask what it holds.

    Compatibility forms are decomposed (a fullwidth letter, a ligature, a
    precomposed shin-dot), the accented spellings above are folded to
    their plain letter, and the marks that may sit between letters are
    removed. Case is left alone for the caller to decide.
    """
    if not text:
        return text
    text = unicodedata.normalize("NFKC", text)
    if text.isascii():
        return text
    out = []
    for ch in text:
        folded = FOLD_TABLE.get(ch)
        if folded is not None:
            out.append(folded)
        elif _MARKS_RE.match(ch):
            continue
        else:
            out.append(ch)
    return "".join(out)


# -- prefixes glued onto a word --------------------------------------------

# Hebrew: the conjunction, the article, the prepositions and the relative
# pronoun, in the combinations that actually occur (ושה-, ובה-, מה-).
HEBREW_PREFIXES = "הובלמשכ"
# Arabic: the article, and the one-letter conjunctions and prepositions
# that go in front of it (وال-, بال-, لل-).
ARABIC_PREFIXES = "وفبلك"
ARABIC_ARTICLE = "ال"

# A word may begin here: not after a letter or a digit, and not after a
# vowel point or accent either, since a mark belongs to the letter before
# it and so is the middle of a word (the damma in أُخرى made خرى look
# like a word of its own). Not \b, because a disguised spelling ends in
# punctuation, which would put a boundary in the wrong place; and not
# [A-Za-z0-9], which is what made every Hebrew letter look like a
# boundary. An underscore is allowed either side because the disguised
# spellings use it as padding.
BOUNDARY_BEFORE = rf"(?<![^\W_])(?<![{MARKS}])"
BOUNDARY_AFTER = r"(?![^\W_])"


def word_start(script: str) -> str:
    """The pattern that must hold before a word of this script begins.

    Plain boundary for most scripts. For Hebrew and Arabic, a boundary
    followed by up to two prefix letters is also a word start, so the
    list entry זונה is found inside והזונה and بورن inside والبورن.
    Every alternative is fixed-width, which is what a lookbehind needs.
    """
    # A pointed text puts a vowel on the prefix letter itself (וְזונה), so
    # each prefix may carry one mark.
    m = f"[{MARKS}]"
    if script == HEBREW:
        p = f"[{HEBREW_PREFIXES}]"
        return (f"(?:{BOUNDARY_BEFORE}"
                f"|(?<={BOUNDARY_BEFORE}{p})"
                f"|(?<={BOUNDARY_BEFORE}{p}{m})"
                f"|(?<={BOUNDARY_BEFORE}{p}{{2}})"
                f"|(?<={BOUNDARY_BEFORE}{p}{m}{p})"
                f"|(?<={BOUNDARY_BEFORE}{p}{p}{m})"
                f"|(?<={BOUNDARY_BEFORE}{p}{m}{p}{m}))")
    if script == ARABIC:
        p = f"[{ARABIC_PREFIXES}]"
        return (f"(?:{BOUNDARY_BEFORE}"
                f"|(?<={BOUNDARY_BEFORE}{p})"
                f"|(?<={BOUNDARY_BEFORE}{p}{m})"
                f"|(?<={BOUNDARY_BEFORE}{ARABIC_ARTICLE})"
                f"|(?<={BOUNDARY_BEFORE}{p}{ARABIC_ARTICLE})"
                f"|(?<={BOUNDARY_BEFORE}{p}{m}{ARABIC_ARTICLE}))")
    return BOUNDARY_BEFORE


# For a matcher that consumes rather than looks behind: the prefixes as an
# optional group to put in front of the word.
PREFIX_GROUP = (f"(?:[{HEBREW_PREFIXES}]{{1,2}}"
                f"|[{ARABIC_PREFIXES}]?{ARABIC_ARTICLE}"
                f"|[{ARABIC_PREFIXES}])?")
