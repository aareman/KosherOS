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
# exactly what it was meant to cover. 0.35 after the first hands-on family
# test: at 0.12 the fringe was still suggestive — the covered area must
# comfortably swallow the detection and its surroundings.
MARGIN = 0.35
# Pixel blocks per region edge. Fewer blocks = coarser = more destroyed;
# 4 leaves only a hint that something was there, which is the point.
BLOCKS = 4


def cover(image_bytes: bytes, regions) -> bytes | None:
    """Return the image with `regions` pixelated, or None if it cannot be done.

    None means the caller should fall back to hiding the whole picture:
    silently returning the original would be the one failure mode that
    matters.
    """
    if not regions:
        return None
    try:
        from PIL import Image
    except ImportError:
        return None
    try:
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
                small = patch.resize((max(1, BLOCKS), max(1, BLOCKS)),
                                     Image.Resampling.BILINEAR)
                image.paste(small.resize(patch.size, Image.Resampling.NEAREST),
                            (left, top))
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
