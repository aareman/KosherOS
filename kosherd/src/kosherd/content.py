"""Scoring the CONTENT of a page or a search result, not just its address.

Domain lists are the backbone of the filter — ten million known-bad hosts,
matched in microseconds — but they only know sites somebody has already
catalogued. The long tail is what gets through: a new domain, a blog post,
a marketplace listing, a search result from a site nobody has classified.
For those, the only thing left to judge is the words on the page.

This is a weighted term scorer rather than a machine-learning model, and
that is a deliberate choice for this product:

* It runs in microseconds on the low-end machine this has to work on, with
  no model to download, no accelerator, and no request leaving the house.
* It can explain itself. When a page is blocked, the admin app can say
  which words did it, and a parent can disagree with a specific word
  instead of arguing with a probability.
* It fails in the direction we can reason about. A model's mistakes are
  arbitrary; a word list's mistakes are a word you can see and remove.

The scoring is weighted and saturating rather than a simple keyword hit,
because a keyword hit is exactly what makes naive filters useless: one
occurrence of "breast" blocks a chicken recipe, and one occurrence of
"sex" blocks a biology lesson. A term contributes less each time it
repeats, terms only count as whole words, and a page has to accumulate
real evidence before it trips.
"""

from __future__ import annotations

import html as _html
import json
import math
import re
from dataclasses import dataclass
from . import lists
from . import textfold
from pathlib import Path

TERMS_PATHS = lists.paths("content-terms.json")

# Severity ladder, matching the media levels a parent chooses from.
CLEAN = "clean"
IMMODEST = "immodest"
SUGGESTIVE = "suggestive"
NSFW = "nsfw"
LEVELS = (CLEAN, IMMODEST, SUGGESTIVE, NSFW)
SEVERITY = {name: i for i, name in enumerate(LEVELS)}

# Every level convicts at the same score; what differs is which terms can
# contribute to it. Points earned at a level also count toward every
# milder level, so explicit words make a page immodest too — but no pile
# of mild words can ever make a page explicit. That asymmetry is the whole
# design: an earlier version summed one flat score, and four words about
# swimwear added up to the same verdict as one word about pornography.
THRESHOLD = 25

# A term stops helping after this many occurrences, so a page cannot be
# convicted by one word repeated in a navigation menu.
MAX_REPEATS = 4

# Writing ABOUT the problem is not the problem. A page on addiction, a
# helpline, a review of filtering software and a shiur on shmiras einayim
# all use the vocabulary of the thing they are warning against, and a
# filter that blocks them is working against the family that installed it.
# So a page in this register has to clear twice the evidence — which a real
# explicit site does several times over, since those score in the hundreds.
#
# The same register in the other languages the lists cover, matched on the
# FOLDED text (see textfold.fold), so the stems are written without their
# accents. A Hebrew stem is found behind the prefixes the language glues
# on (להתמכרות, בסינון), which is what the optional group in front does.
HELP_CONTEXT = re.compile(
    r"(?<![\w-])(?:[" + textfold.HEBREW_PREFIXES + r"]{1,2})?(?:"
    # English, and transliterated Hebrew
    r"addict\w*|recover\w*|rehab\w*|therap\w*|counsel\w*|struggl\w*|"
    r"quit\w*|helpline|hotline|support group|accountability|help|"
    r"filter\w*|block\w*|parental control\w*|safeguard\w*|"
    r"shmiras|shemiras|einayim|teshuv\w*|yetzer|"
    # Hebrew and Yiddish
    r"התמכרות|מכור\w*|גמילה|טיפול|ייעוץ|סינון|מסנן\w*|חסימ\w*|עזרה|"
    r"שמירת עיניים|תשובה|יצר הרע|בקרת הורים|הילף|"
    # Russian and Ukrainian
    r"зависимост\w*|лечени\w*|терапи\w*|помощ\w*|фильтр\w*|блокир\w*|"
    r"родительск\w*|залежн\w*|допомог\w*|"
    # French, Spanish, Portuguese, Italian
    r"aide|dependance|dependencia|dipendenza|ayuda|ajuda|aiuto|terap\w*|"
    r"bloqu\w*|blocc\w*|filtr\w*|controle parental|control parental|"
    r"controllo parentale|adicci\w*|vicio|"
    # German, Dutch, Hungarian, Polish, Turkish
    r"abhangig\w*|hilfe|jugendschutz|kindersicherung|sperr\w*|"
    r"verslaving|hulp|ouderlijk toezicht|fuggoseg|segitseg|szuroprogram|"
    r"uzaleznien\w*|pomoc|bagimlilik|yardim|ebeveyn|"
    # Arabic and Persian
    r"ادمان|علاج|مساعده|حجب|فلتر\w*|رقابه|اعتياد|كمك|درمان"
    r")(?![^\W_])",
    re.IGNORECASE,
)
HELP_CONTEXT_MULTIPLIER = 2


@dataclass(frozen=True)
class Verdict:
    level: str
    points: int
    hits: tuple[str, ...]

    def at_least(self, level: str) -> bool:
        return SEVERITY[self.level] >= SEVERITY[level]


class Scorer:
    """Weighted term scoring over visible text.

    `terms` maps a term to (level, weight): the strongest verdict that term
    can support, and how much evidence it is worth.
    """

    def __init__(self, terms: dict[str, tuple[str, int]] | None = None):
        # Terms are kept in their folded spelling (textfold.fold), which is
        # the spelling the text is matched in: an entry written "cabrón"
        # and a page that writes "cabron" have to meet in the middle.
        self.terms: dict[str, tuple[str, int]] = {}
        for term, value in (terms or {}).items():
            if value[0] not in SEVERITY or value[0] == CLEAN:
                continue
            folded = _canonical(term)
            # Two spellings folding together keep the stronger reading.
            if folded in self.terms and SEVERITY[self.terms[folded][0]] >= SEVERITY[value[0]]:
                continue
            self.terms[folded] = value
        self._regex = self._compile()

    def _compile(self) -> re.Pattern | None:
        if not self.terms:
            return None
        # Longest first so "sex video" scores as the phrase, not as "sex".
        ordered = sorted(self.terms, key=len, reverse=True)
        alts = "|".join(_term_regex(t) for t in ordered)
        # An optional plural on the end: a list written in the singular
        # otherwise misses "crop tops" and "mini skirts", which is how a
        # page of them scored twenty points and passed. The plural is
        # English; the other lists carry their own forms. In front, the
        # prefixes Hebrew and Arabic glue onto a word, so הביקיני and
        # والبورن count as the words they contain.
        return re.compile(
            rf"(?<![\w-]){textfold.PREFIX_GROUP}({alts})(?:e?s)?(?![\w-])",
            re.IGNORECASE)

    def _term_of(self, found: str) -> str | None:
        """The listed term a match was, with the whitespace and any article
        that _term_regex let in between its words taken back out."""
        term = re.sub(r"\s+", " ", found.lower())
        if term in self.terms:
            return term
        for article in (" ה", f" {textfold.ARABIC_ARTICLE}"):
            stripped = term.replace(article, " ")
            if stripped in self.terms:
                return stripped
        return None

    def score(self, text: str, *, allow_help_context: bool = True) -> Verdict:
        if not self._regex or not text:
            return Verdict(CLEAN, 0, ())
        # Matched in the folded spelling the terms are kept in: without
        # points, without the accents people drop, one form per letter.
        text = textfold.fold(text)
        threshold = THRESHOLD
        if allow_help_context and HELP_CONTEXT.search(text):
            threshold *= HELP_CONTEXT_MULTIPLIER
        counts: dict[str, int] = {}
        for match in self._regex.finditer(text):
            term = self._term_of(match.group(1))
            if term is not None:
                counts[term] = counts.get(term, 0) + 1

        earned = {NSFW: 0, SUGGESTIVE: 0, IMMODEST: 0}
        for term, count in counts.items():
            level, weight = self.terms[term]
            # Diminishing returns: the second mention is worth much less
            # than the first, and the tenth is worth nothing.
            earned[level] += round(weight * (1 + math.log(min(count, MAX_REPEATS), 3)))

        # The single-witness rule: ONE distinct term can never convict a
        # page by itself, however often it repeats. A bookstore was blocked
        # as nsfw because its genre menu contains the word "erotica" on
        # every page — one word, repeated by navigation, out-scored the
        # threshold. Anything genuinely objectionable corroborates across
        # several terms; a lone hit is capped just under conviction.
        if len(counts) == 1:
            for level in earned:
                earned[level] = min(earned[level], threshold - 1)

        # Evidence flows downhill: explicit words also make a page immodest.
        cumulative = 0
        level = CLEAN
        for candidate in (NSFW, SUGGESTIVE, IMMODEST):
            cumulative += earned[candidate]
            if cumulative >= threshold:
                level = candidate
                break
        points = cumulative if level != CLEAN else sum(earned.values())
        hits = tuple(sorted(counts, key=lambda t: -self.terms[t][1])[:6])
        return Verdict(level, points, hits)


def _canonical(term: str) -> str:
    """The one spelling a term is stored and matched under."""
    return re.sub(r"\s+", " ", textfold.fold(term).lower()).strip()


def _term_regex(term: str) -> str:
    """A term as a pattern: its words with any whitespace between them.

    Hebrew puts the article on the second word of a phrase (נערות
    הליווי, בגדי הים) and Arabic on both, so between the words of a
    phrase in those scripts the article may appear.
    """
    words = [re.escape(w) for w in term.split()]
    script = textfold.script_of(term)
    if script == textfold.HEBREW:
        joiner = r"\s+ה?"
    elif script == textfold.ARABIC:
        joiner = rf"\s+(?:{textfold.ARABIC_ARTICLE})?"
    else:
        joiner = r"\s+"
    return joiner.join(words)


def is_help_context(text: str) -> bool:
    """Is this text plainly about the problem rather than the material?"""
    return bool(text and HELP_CONTEXT.search(textfold.fold(text)))


def _apply_delta(shipped: dict, add, remove) -> dict:
    """Merge a family's added terms in, and take their removals out.

    `add` is the same shape as the file: {level: {weight: [term, ...]}}.
    `remove` is a flat list of terms, dropped wherever they appear, so a
    family taking a word out does not have to know which level it was on.
    """
    merged = {level: {weight: list(words) for weight, words in by_weight.items()}
              for level, by_weight in shipped.get("terms", {}).items()}
    for level, by_weight in (add or {}).items():
        target = merged.setdefault(level, {})
        for weight, words in by_weight.items():
            target.setdefault(weight, [])
            target[weight] += [w for w in words if w not in target[weight]]
    dropped = {_canonical(t) for t in remove}
    if dropped:
        merged = {level: {weight: [w for w in words if _canonical(w) not in dropped]
                          for weight, words in by_weight.items()}
                  for level, by_weight in merged.items()}
    return {"terms": merged}


def _scorer_from(doc: dict) -> Scorer:
    terms: dict[str, tuple[str, int]] = {}
    for level, by_weight in doc.get("terms", {}).items():
        if level not in SEVERITY or level == CLEAN:
            continue
        for weight, words in by_weight.items():
            try:
                value = int(weight)
            except (TypeError, ValueError):
                continue
            for word in words:
                word = _canonical(word)
                # A term listed twice keeps its strongest reading.
                if word in terms and SEVERITY[terms[word][0]] >= SEVERITY[level]:
                    continue
                terms[word] = (level, value)
    return Scorer(terms)


def load(*paths: Path) -> Scorer:
    """The terms in force: what ships, plus a family's edits on top.

    File shape: {"terms": {"<level>": {"<weight>": [term, ...]}}}
    """
    if paths:
        for path in paths:
            try:
                return _scorer_from(json.loads(Path(path).read_text()))
            except (OSError, ValueError):
                continue
        return Scorer()
    return _scorer_from(lists.resolve("content-terms.json", _apply_delta))


# Everything between < and >, plus script and style bodies: scoring those
# would judge a page by its class names and tracking URLs.
_MARKUP = re.compile(
    r"<script\b[^>]*>.*?</script\s*>|<style\b[^>]*>.*?</style\s*>"
    r"|<!--.*?-->|<[^>]*>",
    re.IGNORECASE | re.DOTALL,
)
_META = re.compile(
    r"<meta[^>]+(?:name|property)\s*=\s*[\"'](?:description|og:description|"
    r"og:title|keywords)[\"'][^>]+content\s*=\s*[\"']([^\"']*)[\"']",
    re.IGNORECASE,
)
_TITLE = re.compile(r"<title[^>]*>(.*?)</title", re.IGNORECASE | re.DOTALL)


def _drop_open_tail(document: str) -> str:
    """Cut off a script, style, comment or tag that opens and never
    closes before the end — what a cut at the scan limit leaves behind.

    The FIRST unclosed opener is where the cut goes, not the last: a
    minified script can contain the text "<script" in a string, and
    cutting there would keep the kilobytes of source before it.
    """
    lowered = document.lower()
    cut = len(document)
    for opener, closer in (("<script", "</script"), ("<style", "</style"),
                           ("<!--", "-->")):
        pos = 0
        while True:
            start = lowered.find(opener, pos, cut)
            if start == -1:
                break
            end = lowered.find(closer, start + len(opener), cut)
            if end == -1:
                cut = start
                break
            pos = end + len(closer)
    start = lowered.rfind("<", 0, cut)
    if start != -1 and lowered.find(">", start, cut) == -1:
        cut = start
    return document[:cut]


def visible_text(html: str, limit: int = 200_000) -> str:
    """The words a reader would see, plus the title and description.

    The description meta tags are included on purpose: they are often the
    most honest summary of what a page is, and they are what a search
    engine shows as the snippet.
    """
    # The cut at the limit lands wherever it lands — and callers often
    # slice before calling, so it may already have landed. If that is
    # inside a script, a stylesheet, a comment or a tag, everything after
    # its opening is source that never reached a reader, and left in
    # place it is scored as words ("xxx-large", "1 ul", SVG path data).
    document = _drop_open_tail(html[:limit])
    parts = [m.group(1) for m in _TITLE.finditer(document)]
    parts += [m.group(1) for m in _META.finditer(document)]
    parts.append(_MARKUP.sub(" ", document))
    # Entities spell letters too: a page that writes &eacute; or &#1489;
    # is read as the letters it means, not as the markup it used.
    return _html.unescape(" ".join(parts))


def score_html(html: str, scorer: Scorer) -> Verdict:
    return scorer.score(visible_text(html))
