"""Inspect generated images embedded in provider responses."""

import base64
import binascii
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

from .ai_text import OutputBlocked
from .vision import ImageVerdict, hides

MAX_IMAGE_BYTES: Final = 12 * 1024 * 1024
BLANK_IMAGE: Final = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


@dataclass(frozen=True, slots=True)
class ImageFilter:
    """Apply the account's existing image level before an inline image escapes.

    The judge is supplied by the proxy's existing image detector. Refused or
    uncheckable images become a transparent PNG, including partial previews.
    """

    level: str
    judge: Callable[[bytes], ImageVerdict | None]

    def base64(self, encoded: str) -> str:
        """Return a checked image or a placeholder, never malformed image data."""
        if self.level == "none":
            return encoded
        if self.level == "all":
            return BLANK_IMAGE
        if len(encoded) > MAX_IMAGE_BYTES * 4 // 3 + 4:
            raise OutputBlocked("Generated image exceeds the inspection limit")
        try:
            decoded = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error) as error:
            raise OutputBlocked("Generated image has invalid base64 data") from error
        verdict = self.judge(decoded)
        if verdict is None or hides(self.level, verdict):
            return BLANK_IMAGE
        return encoded

    def data_uri(self, uri: str) -> str:
        """Inspect an inline image without trusting its declared image type."""
        header, separator, encoded = uri.partition(",")
        if not separator or not header.lower().endswith(";base64"):
            raise OutputBlocked("Unsupported generated image encoding")
        filtered = self.base64(encoded)
        if filtered == BLANK_IMAGE:
            return "data:image/png;base64," + filtered
        return uri
