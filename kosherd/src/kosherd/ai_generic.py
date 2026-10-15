"""Streamed text of a shape nobody taught us, checked anyway.

There are more AI sites than anyone can write protocol handlers for, and
most of them stream their answers as JSON events of their own design. What
they have in common is that the answer is prose inside strings. So, for an
event of unknown shape: every string that reads as prose (it has
whitespace in it, or a listed word) gets the account's language filter, the
prose of the whole stream is scored together, and inline pictures are
checked. Identifiers, tokens and URLs have no whitespace and are left alone.

What this cannot do, and the known shapes can: hold back the tail of a
delta so a word split between two events is caught, and attach the right
refusal notice. A refusal here ends the stream.
"""

from typing import Final, final

from .ai_json import EMBEDDED_JSON, IMAGE_FIELDS, MAX_DEPTH, ContentFilter, dumps, parse
from .ai_text import OutputBlocked

SCORE_WINDOW: Final = 200_000


@final
class GenericText:
    """One stream's prose, checked string by string and scored as a whole."""

    def __init__(self, filters: ContentFilter) -> None:
        self.filters = filters
        self.context = ""

    def prose(self, value: str) -> bool:
        """Words with whitespace between them, or one bare word on the list.

        An identifier such as "abc_damn_123" has no whitespace and is not a
        word, so it is left alone; a delta of a single word is not."""
        stripped = value.strip()
        if not stripped:
            return False
        if any(ch.isspace() for ch in stripped):
            return True
        return stripped.isalpha() and self.filters.text.words.contains_any(stripped)

    def string(self, value: str) -> str:
        """Check one string on its own and in the light of the stream so far."""
        checked = self.filters.string(value)
        self.context = (self.context + "\n" + value)[-SCORE_WINDOW:]
        self.filters.text.check(self.context)
        return checked

    def text(self, value: str) -> str:
        """Plain text, not JSON: the whole payload is prose."""
        return self.string(value) if self.prose(value) else value

    def clean(self, value, field: str = "", depth: int = 0):
        if depth > MAX_DEPTH:
            raise OutputBlocked("AI payload nests too deeply")
        if isinstance(value, str):
            if field in EMBEDDED_JSON:
                try:
                    inner = parse(value)
                except OutputBlocked:
                    return self.text(value)
                return dumps(self.clean(inner, "", depth + 1))
            if field in IMAGE_FIELDS:
                return self.filters.images.base64(value)
            if value.lower().startswith("data:image/"):
                return self.filters.images.data_uri(value)
            return self.string(value) if self.prose(value) else value
        if isinstance(value, list):
            return [self.clean(item, field, depth + 1) for item in value]
        if isinstance(value, dict):
            return {key: self.clean(item, str(key), depth + 1) for key, item in value.items()}
        return value
