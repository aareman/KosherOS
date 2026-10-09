"""Inline generated pictures must pass image policy before release."""

import base64

import pytest
from kosherd.ai_images import BLANK_IMAGE, ImageFilter
from kosherd.ai_text import OutputBlocked
from kosherd.vision import ImageVerdict


@pytest.mark.parametrize("verdict", [None, ImageVerdict("nsfw")])
def test_refused_or_uncheckable_image_becomes_placeholder(verdict: ImageVerdict | None) -> None:
    # Given image bytes inside an API JSON field.
    encoded = base64.b64encode(b"generated image fixture").decode()
    image_filter = ImageFilter("immodest", lambda _: verdict)
    # When the detector refuses the image or cannot check it.
    result = image_filter.base64(encoded)
    # Then only a harmless image reaches the client.
    assert result == BLANK_IMAGE


def test_clean_image_is_preserved_byte_for_byte() -> None:
    # Given a detector verdict permitting an inline image.
    encoded = base64.b64encode(b"generated image fixture").decode()
    image_filter = ImageFilter("immodest", lambda _: ImageVerdict("clean"))
    # When checked, then the original encoding is preserved.
    assert image_filter.base64(encoded) == encoded


def test_hidden_data_uri_changes_mime_type_to_png() -> None:
    # Given an inline JPEG under the hide-all policy.
    image_filter = ImageFilter("all", lambda _: None)
    # When hidden, then the replacement declares its actual format.
    assert image_filter.data_uri("data:image/jpeg;base64,YQ==") == (
        "data:image/png;base64," + BLANK_IMAGE
    )


def test_malformed_encoding_is_refused() -> None:
    # Given a generated-image field with invalid base64.
    image_filter = ImageFilter("immodest", lambda _: ImageVerdict("clean"))
    # When parsed, then malformed data is never treated as a clean image.
    with pytest.raises(OutputBlocked):
        image_filter.base64("%not-base64%")
