"""Covering part of a picture instead of removing all of it.

Blanking an image is the safe answer and a bad experience: a page whose
pictures became empty boxes looks broken, and people route around things
that look broken. When the detector says *where* the problem is, covering
just that region leaves the rest of the picture — and the page — intact.

Generative inpainting (repainting what was there) is what the ask sounds
like, and it is not on the table: the models are hundreds of megabytes and
take seconds per image on a GPU, on a product whose whole premise is that
it works on a weak machine with no network round trip. The cover here is a
heavy frosted blur with light noise under a feathered edge: instant, needs
nothing but Pillow, and does not pretend the picture was something it was
not.
"""

from __future__ import annotations

import io
import logging
from collections import OrderedDict

log = logging.getLogger(__name__)

# Grow each region by this fraction of its size before covering it. The
# detector's boxes are tight, and a tight box leaves a visible fringe of
# exactly what it was meant to cover. Generous, after three rounds of family
# feedback that the covered area was too small.
MARGIN = 0.6

# How far the cover reduces a region before smoothing it back up: the short
# side of the patch shrinks to this many pixels. A Gaussian blur alone, at
# any radius that keeps the page fast, left a figure's silhouette and skin
# tone readable — "blur is not so effective". Shrinking the patch to a
# handful of pixels first throws the shape away entirely, and costs less
# than the blur it replaces because the blur then runs on a thumbnail.
FROST_CELLS = 6
# How much the cover is pulled toward the region's own average colour,
# which flattens what contrast survives without stamping a flat block.
FLATTEN = 0.35
# Noise under the blur, so the cover cannot be undone by deconvolution.
NOISE = 0.10

# When the covered area is this much of the picture, cover nothing and hide
# the picture: a frame of background around a frosted rectangle helps nobody
# and costs a decode, a blur and a re-encode.
DOMINANT = 0.6

# The two cover styles (policy.COVER_STYLES).
FROST = "frost"
SKIN = "skin"
# The skin style paints skin-toned pixels inside the detected figure this
# solid colour, through a mask grown by SKIN_EXPAND of the region's short
# side so the edge of a limb does not show. The gate is the classic YCbCr
# one, which is tuned on colour and does not see every skin tone equally;
# where it finds less than SKIN_MINIMUM of the region to paint, the region
# is frosted instead, so a person the gate cannot see is still covered.
SKIN_FILL = (118, 118, 118)
SKIN_EXPAND = 0.08
SKIN_MINIMUM = 0.04
# The mask is computed on a copy no larger than this on its long side: a
# dilation over a full-size photo is slow, and a mask needs no detail.
SKIN_MASK_MAX = 256


def cover(image_bytes: bytes, regions, style: str = FROST) -> bytes | None:
    """Return the image with `regions` covered, or None if it cannot be done.

    `style` is FROST (the figure frosted) or SKIN (skin inside the figure
    painted solid; see the constants above). None means the caller should
    fall back to hiding the whole picture: silently returning the original
    would be the one failure mode that matters.
    """
    if not regions:
        return None
    try:
        from PIL import Image, ImageDraw, ImageFilter, ImageStat
    except ImportError:
        return None
    try:
        import os

        with Image.open(io.BytesIO(image_bytes)) as source:
            fmt = (source.format or "PNG").upper()
            image = source.convert("RGB") if source.mode not in ("RGB", "RGBA") \
                else source.copy()
            width, height = image.size
            for x, y, w, h in regions:
                left, top, right, bottom = _grow(x, y, w, h, width, height)
                if right <= left or bottom <= top:
                    continue
                patch = image.crop((left, top, right, bottom))
                pw, ph = patch.size
                if style == SKIN:
                    mask = skin_mask(patch)
                    if mask is not None:
                        fill = Image.new("RGB", (pw, ph), SKIN_FILL).convert(image.mode)
                        image.paste(fill, (left, top), mask)
                        continue
                    # Nothing the gate could see: frost the region instead.
                # Frost: down to a few cells and back up, then smooth the
                # cell edges. All the shape information goes in the first
                # step; the rest is making it sit quietly in the picture.
                cells = max(2, FROST_CELLS)
                small = patch.resize(
                    (max(cells, int(pw * cells / max(1, min(pw, ph)))),
                     max(cells, int(ph * cells / max(1, min(pw, ph))))),
                    Image.Resampling.BOX)
                frosted = small.resize((pw, ph), Image.Resampling.BICUBIC)
                frosted = frosted.filter(ImageFilter.GaussianBlur(max(8, min(pw, ph) // 6)))
                mean = tuple(int(v) for v in ImageStat.Stat(patch.convert("RGB")).mean)
                flat = Image.new("RGB", (pw, ph), mean).convert(frosted.mode)
                covered = Image.blend(frosted, flat, FLATTEN)
                noise = Image.frombytes("L", (pw, ph),
                                        os.urandom(pw * ph)).convert(covered.mode)
                covered = Image.blend(covered, noise, NOISE)
                covered = covered.filter(ImageFilter.GaussianBlur(4))
                # Feather: a soft-edged mask melts the cover into the photo
                # instead of stamping a hard rectangle on it.
                feather = max(6, min(pw, ph) // 8)
                mask = Image.new("L", (pw, ph), 0)
                ImageDraw.Draw(mask).rectangle(
                    (feather, feather, pw - feather, ph - feather), fill=255)
                mask = mask.filter(ImageFilter.GaussianBlur(feather))
                image.paste(covered, (left, top), mask)
            return encode(image, fmt)
    except Exception:  # noqa: BLE001 - a picture we cannot edit gets hidden
        log.debug("could not cover regions", exc_info=True)
        return None


def skin_mask(patch):
    """A mask of the skin-toned pixels in `patch`, grown so a limb's edge is
    inside it — or None when there is too little skin for a paint to be the
    right cover (see SKIN_MINIMUM)."""
    from PIL import Image, ImageChops, ImageFilter

    pw, ph = patch.size
    scale = min(1.0, SKIN_MASK_MAX / max(1, max(pw, ph)))
    small = patch.convert("YCbCr")
    if scale < 1.0:
        small = small.resize((max(1, int(pw * scale)), max(1, int(ph * scale))),
                             Image.Resampling.BILINEAR)
    y, cb, cr = small.split()
    gate = ImageChops.multiply(
        ImageChops.multiply(y.point(lambda v: 255 if v > 60 else 0),
                            cb.point(lambda v: 255 if 77 <= v <= 127 else 0)),
        cr.point(lambda v: 255 if 133 <= v <= 173 else 0))
    if not gate.getbbox():
        return None
    coverage = sum(1 for v in gate.getdata() if v) / max(1, gate.width * gate.height)
    if coverage < SKIN_MINIMUM:
        return None
    # Expand: dilate by a fraction of the region's short side, at mask scale.
    grow = max(3, int(min(gate.size) * SKIN_EXPAND)) | 1
    grown = gate.filter(ImageFilter.MaxFilter(grow))
    # Soften the staircase the dilation leaves, then harden it again.
    grown = grown.filter(ImageFilter.GaussianBlur(1.5)).point(lambda v: 255 if v > 96 else 0)
    if grown.size != (pw, ph):
        grown = grown.resize((pw, ph), Image.Resampling.BILINEAR).point(
            lambda v: 255 if v > 128 else 0)
    return grown


def encode(image, fmt: str) -> bytes:
    """Write the picture back in its own format where that is cheap.

    WebP came back as PNG before, which for a photograph is five to ten
    times the bytes and a slow encode; JPEG stays JPEG, WebP stays WebP,
    everything else (PNG, GIF, the odd BMP) becomes PNG.
    """
    out = io.BytesIO()
    if fmt in ("JPEG", "JPG", "MPO"):
        image.convert("RGB").save(out, "JPEG", quality=85)
    elif fmt == "WEBP":
        image.save(out, "WEBP", quality=80, method=0)
    else:
        image.save(out, "PNG", compress_level=1)
    return out.getvalue()


def content_type(data: bytes) -> str:
    if data[:2] == b"\xff\xd8":
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/png"


def dominant(image_bytes: bytes, regions, threshold: float = DOMINANT) -> bool:
    """Would covering `regions` (grown as cover() grows them) take most of
    the picture? Reads only the header, so it costs almost nothing."""
    if not regions:
        return False
    try:
        from PIL import Image

        with Image.open(io.BytesIO(image_bytes)) as im:
            width, height = im.size
    except Exception:  # noqa: BLE001 - unreadable: the caller will hide it anyway
        return False
    if width <= 0 or height <= 0:
        return False
    # Rasterise the union coarsely rather than sum overlapping boxes.
    step = max(1, min(width, height) // 64)
    covered = 0
    total = 0
    boxes = [_grow(x, y, w, h, width, height) for x, y, w, h in regions]
    for py in range(0, height, step):
        for px in range(0, width, step):
            total += 1
            if any(l <= px < r and t <= py < b for l, t, r, b in boxes):
                covered += 1
    return total > 0 and covered / total >= threshold


def _grow(x, y, w, h, width, height):
    dx, dy = int(w * MARGIN), int(h * MARGIN)
    return (max(0, x - dx), max(0, y - dy),
            min(width, x + w + dx), min(height, y + h + dy))


class CoverCache:
    """Recently covered pictures, by content hash, so a page reload or the
    same photo on the next page does not decode, frost and re-encode it
    again. Small and in memory: the verdict cache on disk is what remembers
    the judgement; this only remembers the pixels for a little while."""

    def __init__(self, max_bytes: int = 24 * 1024 * 1024):
        self.max_bytes = max_bytes
        self._items: OrderedDict[str, bytes] = OrderedDict()
        self._size = 0

    def get(self, key: str) -> bytes | None:
        data = self._items.get(key)
        if data is not None:
            self._items.move_to_end(key)
        return data

    def put(self, key: str, data: bytes) -> None:
        if len(data) > self.max_bytes // 4:
            return
        if key in self._items:
            self._size -= len(self._items.pop(key))
        self._items[key] = data
        self._size += len(data)
        while self._size > self.max_bytes and self._items:
            _k, old = self._items.popitem(last=False)
            self._size -= len(old)

    def __len__(self) -> int:
        return len(self._items)
