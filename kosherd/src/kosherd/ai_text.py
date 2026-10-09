"""Text policy for generated output, with a retained streaming lookahead."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Final, final

from .content import Scorer
from .language import Wordlist

MAX_TEXT_CHARS: Final = 1_000_000


class OutputBlocked(ValueError):
    """Generated output cannot be released under the account's policy.

    `reason` is the short machine-readable word the activity log carries:
    "language", "content", "image" or "protocol".
    """

    def __init__(self, message: str, *, reason: str = "protocol") -> None:
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class TextPolicy:
    """The account's existing choices, applied to plain generated text.

    `language` is the account's language filter ("off", "substitute" or
    "block"); `tolerance` is the content level at which an answer is refused
    (None scores nothing).
    """

    language: str
    tolerance: str | None


@final
class TextFilter:
    """Share immutable policy and dictionaries across response-local buffers."""

    def __init__(self, policy: TextPolicy, dictionaries: tuple[Wordlist, Scorer]) -> None:
        self.policy = policy
        self.words, self.scorer = dictionaries
        # Listed disguises allow punctuation and spaces between letters.
        # Retain substantially more than the longest configured term.
        terms = (*self.words.replacements, *self.scorer.terms)
        self.lookahead = max(256, max(map(len, terms), default=0) * 8 + 64)

    def check(self, text: str) -> str:
        """Judge the entire supplied context before returning any replacement."""
        if len(text) > MAX_TEXT_CHARS:
            raise OutputBlocked("AI text exceeds the inspection limit")
        if self.policy.language == "block" and self.words.contains_any(text):
            raise OutputBlocked("AI output blocked by language policy", reason="language")
        if self.policy.tolerance is not None and self.scorer.score(text).at_least(self.policy.tolerance):
            raise OutputBlocked("AI output blocked by content policy", reason="content")
        if self.policy.language == "substitute":
            return self.words.clean(text)[0]
        return text

    def withhold(self, text: str) -> str:
        """A snapshot with its unfinished tail held back, cut at a word boundary."""
        end = len(text) - self.lookahead
        if end <= 0:
            return ""
        boundary = text.rfind(" ", 0, end)
        return text[:boundary + 1] if boundary >= 0 else ""


@final
class TextStream:
    """Accumulate context and retain a tail before releasing checked text.

    State belongs to one output content block, never to a connection or user.
    Earlier released text cannot be recalled when later context changes a score.
    """

    def __init__(self, checker: TextFilter, clean: Callable[[str], str] | None = None) -> None:
        self.checker = checker
        self.clean = clean or checker.check
        self._raw = ""
        self._released = ""

    @property
    def released(self) -> str:
        return self._released

    def push(self, text: str) -> str:
        self._raw += text
        cleaned = self.clean(self._raw)
        if not cleaned.startswith(self._released):
            raise OutputBlocked("AI text changed previously checked context")
        end = len(cleaned) - self.checker.lookahead
        if end <= len(self._released):
            return ""
        # Do not expose the first half of an ordinary word.
        boundary = cleaned.rfind(" ", len(self._released), end)
        if boundary < len(self._released):
            return ""
        released = cleaned[len(self._released):boundary + 1]
        self._released = cleaned[:boundary + 1]
        return released

    def finish(self) -> str:
        cleaned = self.clean(self._raw)
        if not cleaned.startswith(self._released):
            raise OutputBlocked("AI text changed previously checked context")
        released = cleaned[len(self._released):]
        self._released = cleaned
        return released
