"""JSON parsing and content-field inspection for AI browser traffic.

Provider payloads are walked once. Only the fields that carry text a person
will read, or a picture they will see, are changed; identifiers, revisions
and protocol values pass through untouched so the page keeps working.
"""

import json
import re
from dataclasses import dataclass
from typing import Final

from .ai_images import ImageFilter
from .ai_text import OutputBlocked, TextFilter

DATA_IMAGE: Final = re.compile(r"data:image/[^\s\"'<>),]+,[A-Za-z0-9+/=]+", re.IGNORECASE)
# Fields whose string value is prose (or markup around prose).
TEXT_FIELDS: Final = frozenset({
    "text", "thinking", "completion", "title", "name", "description",
    "code", "markdown", "fallbackMarkdown", "html", "snippet", "caption",
    "alt", "message", "summary", "answer", "reasoning", "parts", "content",
})
# Objects whose every string value is prose, whatever the key.
TEXT_TABLES: Final = frozenset({"constants"})
# Strings that hold a JSON document of their own. Everything inside one is
# treated as prose: its keys are the provider's, not ours to know.
EMBEDDED_JSON: Final = frozenset({"conversationState"})
ALL_TEXT: Final = "*"
IMAGE_FIELDS: Final = frozenset({"b64_json", "partial_image_b64"})
MAX_DEPTH: Final = 64


def parse(data: bytes | str):
    """One JSON document, or a refusal: a payload we cannot read is not released."""
    try:
        return json.loads(data)
    except (ValueError, RecursionError, UnicodeError) as error:
        raise OutputBlocked("AI payload is not valid JSON") from error


def dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def mapping(value) -> dict:
    """Require an object at a provider protocol boundary."""
    if isinstance(value, dict):
        return value
    raise OutputBlocked("Unsupported AI response structure")


@dataclass(frozen=True, slots=True)
class ContentFilter:
    text: TextFilter
    images: ImageFilter

    def tool(self, value) -> None:
        """Refuse unsafe tool arguments without rewriting executable source."""
        if self.clean(value) != value:
            raise OutputBlocked("AI tool output requires a content change")
        if isinstance(value, str):
            if self.string(value) != value:
                raise OutputBlocked("AI tool output blocked by language policy",
                                    reason="language")
        elif isinstance(value, list):
            for item in value:
                self.tool(item)
        elif isinstance(value, dict):
            for item in value.values():
                self.tool(item)

    def string(self, value: str) -> str:
        """Inspect inline images before applying the plain-text policy."""
        if value.lower().count("data:image/") != len(DATA_IMAGE.findall(value)):
            raise OutputBlocked("Unsupported inline generated image", reason="image")
        checked = DATA_IMAGE.sub(lambda match: self.images.data_uri(match.group()), value)
        return self.text.check(checked)

    def clean(self, value, field: str = "", depth: int = 0):
        """Inspect content fields while retaining provider IDs and protocol values."""
        if depth > MAX_DEPTH:
            raise OutputBlocked("AI payload nests too deeply")
        if isinstance(value, str):
            if field in EMBEDDED_JSON:
                try:
                    inner = json.loads(value)
                except ValueError:
                    return self.string(value)
                return dumps(self.clean(inner, ALL_TEXT, depth + 1))
            if field in TEXT_FIELDS or field == ALL_TEXT:
                return self.string(value)
            if field in IMAGE_FIELDS:
                return self.images.base64(value)
            if value.lower().startswith("data:image/"):
                return self.images.data_uri(value)
            return value
        if isinstance(value, list):
            return [self.clean(item, field, depth + 1) for item in value]
        if isinstance(value, dict):
            result = {}
            for key, item in value.items():
                child = ALL_TEXT if field == ALL_TEXT else "text" if field in TEXT_TABLES else str(key)
                result[key] = self.clean(item, child, depth + 1)
            data = value.get("data")
            if value.get("type") == "base64" and isinstance(data, str):
                result["data"] = self.images.base64(data)
                if result["data"] != data:
                    result["media_type"] = "image/png"
            return result
        return value

    def blank(self, value, field: str = "", depth: int = 0):
        """The same walk as clean, with every piece of prose emptied.

        For a line that cannot be released as it is but that the page needs
        in order to finish (a template, a state record)."""
        if depth > MAX_DEPTH:
            raise OutputBlocked("AI payload nests too deeply")
        if isinstance(value, str):
            if field in EMBEDDED_JSON:
                try:
                    inner = json.loads(value)
                except ValueError:
                    return ""
                return dumps(self.blank(inner, ALL_TEXT, depth + 1))
            if field in TEXT_FIELDS or field == ALL_TEXT or field in IMAGE_FIELDS \
                    or value.lower().startswith("data:image/"):
                return ""
            return value
        if isinstance(value, list):
            return [self.blank(item, field, depth + 1) for item in value]
        if isinstance(value, dict):
            return {key: self.blank(item, ALL_TEXT if field == ALL_TEXT else "text" if field in TEXT_TABLES
                                    else str(key), depth + 1)
                    for key, item in value.items()}
        return value
