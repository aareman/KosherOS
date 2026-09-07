"""Covering part of a picture instead of removing all of it.

Blanking an image is the safe answer and a bad experience: a page whose
pictures became empty boxes looks broken, and people route around things
that look broken. When the detector says *where* the problem is, covering
just that region leaves the rest of the picture — and the page — intact.

Generative inpainting (repainting what was there) is what the ask sounds
like, and it is not on the table: the models are hundreds of megabytes and
take seconds per image on a GPU, on a product whose whole premise is that
it works on a weak machine with no network round trip. A heavy pixelation
is instant, needs nothing but Pillow, and does not pretend the picture was
something it was not.
"""

from __future__ import annotations

import io
import logging

log = logging.getLogger(__name__)

# Grow each region by this fraction of its size before covering it. The
# detector's boxes are tight, and a tight box leaves a visible fringe of
# exactly what it was meant to cover. Generous, after two rounds of family
# feedback that the covered area was too small.
MARGIN = 0.5


def cover(image_bytes: bytes, regions) -> bytes | None:
    """Return the image with `regions` covered, or None if it cannot be done.

    The cover is a heavy blur with light noise underneath, composited
    through a feathered mask — after family feedback in three steps: the
    tight pixel blocks left a fringe; blocks plus static destroyed the
    content but shouted about it; this destroys as much and sits quietly in
    the picture. Blur radius scales with the region, noise stops the blur
    being invertible, and the feather melts the edges into the photo.

    None means the caller should fall back to hiding the whole picture:
    silently returning the original would be the one failure mode that
    matters.
    """
    if not regions:
        return None
    try:
        from PIL import Image, ImageDraw, ImageFilter
    except ImportError:
        return None
    try:
        import io
        import os

        with Image.open(io.BytesIO(image_bytes)) as source:
            image = source.convert("RGB") if source.mode not in ("RGB", "RGBA") \
                else source.copy()
            fmt = source.format or "PNG"
            width, height = image.size
            for x, y, w, h in regions:
                left, top, right, bottom = _grow(x, y, w, h, width, height)
                if right <= left or bottom <= top:
                    continue
                patch = image.crop((left, top, right, bottom))
                pw, ph = patch.size
                radius = max(12, min(pw, ph) // 4)
                blurred = patch.filter(ImageFilter.GaussianBlur(radius))
                noise = Image.frombytes("L", (pw, ph),
                                        os.urandom(pw * ph)).convert(blurred.mode)
                covered = Image.blend(blurred, noise, 0.12)
                covered = covered.filter(ImageFilter.GaussianBlur(radius // 2))
                # Feather: a soft-edged mask melts the cover into the photo
                # instead of stamping a hard rectangle on it.
                feather = max(6, min(pw, ph) // 8)
                mask = Image.new("L", (pw, ph), 0)
                ImageDraw.Draw(mask).rectangle(
                    (feather, feather, pw - feather, ph - feather), fill=255)
                mask = mask.filter(ImageFilter.GaussianBlur(feather))
                image.paste(covered, (left, top), mask)
            out = io.BytesIO()
            if fmt.upper() in ("JPEG", "JPG"):
                image.convert("RGB").save(out, "JPEG", quality=85)
            else:
                image.save(out, "PNG")
            return out.getvalue()
    except Exception:  # noqa: BLE001 - a picture we cannot edit gets hidden
        log.debug("could not cover regions", exc_info=True)
        return None


def _grow(x, y, w, h, width, height):
    dx, dy = int(w * MARGIN), int(h * MARGIN)
    return (max(0, x - dx), max(0, y - dy),
            min(width, x + w + dx), min(height, y + h + dy))
