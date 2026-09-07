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

import json
import math
import re
from dataclasses import dataclass
from . import lists
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
HELP_CONTEXT = re.compile(
    r"\b(addict\w*|recover\w*|rehab\w*|therap\w*|counsel\w*|struggl\w*|"
    r"quit\w*|helpline|hotline|support group|accountability|help|"
    r"filter\w*|block\w*|parental control\w*|safeguard\w*|"
    r"shmiras|shemiras|einayim|teshuv\w*|yetzer)\b",
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
        self.terms = {t: v for t, v in (terms or {}).items()
                      if v[0] in SEVERITY and v[0] != CLEAN}
        self._regex = self._compile()

    def _compile(self) -> re.Pattern | None:
        if not self.terms:
            return None
        # Longest first so "sex video" scores as the phrase, not as "sex".
        ordered = sorted(self.terms, key=len, reverse=True)
        alts = "|".join(re.escape(t).replace(r"\ ", r"\s+") for t in ordered)
        # An optional plural on the end: a list written in the singular
        # otherwise misses "crop tops" and "mini skirts", which is how a
        # page of them scored twenty points and passed.
        return re.compile(rf"(?<![\w-])({alts})(?:e?s)?(?![\w-])",
                          re.IGNORECASE)

    def score(self, text: str, *, allow_help_context: bool = True) -> Verdict:
        if not self._regex or not text:
            return Verdict(CLEAN, 0, ())
        threshold = THRESHOLD
        if allow_help_context and HELP_CONTEXT.search(text):
            threshold *= HELP_CONTEXT_MULTIPLIER
        counts: dict[str, int] = {}
        for match in self._regex.finditer(text):
            term = re.sub(r"\s+", " ", match.group(1).lower())
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
    dropped = {t.lower() for t in remove}
    if dropped:
        merged = {level: {weight: [w for w in words if w.lower() not in dropped]
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
                word = word.lower()
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


def visible_text(html: str, limit: int = 200_000) -> str:
    """The words a reader would see, plus the title and description.

    The description meta tags are included on purpose: they are often the
    most honest summary of what a page is, and they are what a search
    engine shows as the snippet.
    """
    html = html[:limit]
    parts = [m.group(1) for m in _TITLE.finditer(html)]
    parts += [m.group(1) for m in _META.finditer(html)]
    parts.append(_MARKUP.sub(" ", html))
    return " ".join(parts)


def score_html(html: str, scorer: Scorer) -> Verdict:
    return scorer.score(visible_text(html))
