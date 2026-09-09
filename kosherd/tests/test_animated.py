"""Pictures that move: GIFs, animated PNGs and WebPs.

Before this, a GIF was always hidden (the detector's image reader cannot
open one), and an animated PNG or WebP was judged on its first frame alone,
so a clean opening frame let the rest through. Now an animation is sampled
across its frames like a short clip, gets one verdict for the lot, and is
hidden whole when it hides — a cover on one frame means nothing on the
others. WebM is video and takes the video path.
"""

import io

import pytest

from kosherd import imageedit, vision
from kosherd.vision import CLEAN, NSFW, Detection, ImageVerdict

Image = pytest.importorskip("PIL.Image")

GREEN, RED = (10, 200, 10), (220, 20, 20)


def _frame(colour, size=(320, 320)):
    """A frame that is unmistakably `colour` on average but not a flat fill:
    a flat fill compresses under the 2.5 KB icon floor and is never judged,
    so a sixth of the pixels are shifted a little, pseudo-randomly."""
    import random

    im = Image.new("RGB", size, colour)
    r, g, b = colour
    px = im.load()
    rng = random.Random(size[0] * 7 + r)
    for _ in range(size[0] * size[1] // 6):
        x, y = rng.randrange(size[0]), rng.randrange(size[1])
        d = rng.randrange(-24, 25)
        px[x, y] = (max(0, min(255, r + d)), max(0, min(255, g - d)), max(0, min(255, b + d)))
    return im


def _animation(colours, fmt="GIF"):
    frames = [_frame(c) for c in colours]
    out = io.BytesIO()
    kwargs = {"save_all": True, "append_images": frames[1:], "duration": 100, "loop": 0}
    if fmt == "GIF":
        frames[0].save(out, "GIF", **kwargs)
    elif fmt == "PNG":
        frames[0].save(out, "PNG", **kwargs)
    else:
        # Lossless, or lossy WebP squeezes the noise under the icon floor.
        frames[0].save(out, "WEBP", lossless=True, **kwargs)
    data = out.getvalue()
    assert len(data) >= vision.MIN_IMAGE_BYTES, (fmt, len(data))
    return data


def _still(colour, fmt):
    out = io.BytesIO()
    _frame(colour).save(out, fmt)
    return out.getvalue()


class ColourDetector:
    """Flags a frame as explicit when it is mostly red; sees what it is handed."""

    available = True

    def __init__(self):
        self.seen = []

    def detect(self, data):
        self.seen.append(data[:4])
        with Image.open(io.BytesIO(data)) as im:
            r, g, b = im.convert("RGB").resize((1, 1)).getpixel((0, 0))
        if r > 150 and g < 80:
            return [Detection("FEMALE_BREAST_EXPOSED", 0.9, (10, 10, 40, 40))]
        return []


def _filter(tmp_path):
    det = ColourDetector()
    return vision.ImageFilter(detector=det, cache=vision.VerdictCache(tmp_path / "i.sqlite")), det


@pytest.mark.parametrize("fmt", ["GIF", "PNG", "WEBP"])
def test_a_bad_frame_late_in_an_animation_is_found(tmp_path, fmt):
    data = _animation([GREEN, GREEN, GREEN, GREEN, GREEN, GREEN, GREEN, RED], fmt)
    assert imageedit.is_animated(data)
    f, det = _filter(tmp_path)
    verdict = f.verdict(data)
    assert verdict is not None and verdict.level == NSFW, fmt
    # Several frames were judged, spread through the animation, and every
    # one reached the detector as a JPEG it can read.
    assert 2 <= len(det.seen) <= vision.ANIMATION_FRAMES
    assert all(chunk[:2] == b"\xff\xd8" for chunk in det.seen)
    assert verdict.regions == (), "no region: an animation is hidden whole"


def test_a_clean_animation_is_clean(tmp_path):
    f, _det = _filter(tmp_path)
    verdict = f.verdict(_animation([GREEN] * 6))
    assert verdict is not None and verdict.level == CLEAN


def test_a_still_gif_is_judged_not_refused(tmp_path):
    # OpenCV cannot open a GIF, so every GIF used to come back "could not
    # judge" and be hidden. The detector is handed a JPEG of it instead.
    f, _det = _filter(tmp_path)
    assert f.verdict(_still(GREEN, "GIF")).level == CLEAN
    assert f.verdict(_still(RED, "GIF")).level == NSFW
    assert not imageedit.is_animated(_still(GREEN, "GIF"))
    prepared, scale = vision.prepare(_still(RED, "GIF"))
    assert prepared[:2] == b"\xff\xd8"


def test_stills_the_reader_can_open_are_handed_over_as_they_are():
    big = io.BytesIO()
    _frame(GREEN, (800, 600)).save(big, "PNG")
    prepared, scale = vision.prepare(big.getvalue())
    assert prepared == big.getvalue() and scale == 1.0, "no needless re-encode"
    small = io.BytesIO()
    _frame(GREEN, (200, 150)).save(small, "PNG")
    prepared, scale = vision.prepare(small.getvalue())
    assert prepared[:2] == b"\xff\xd8" and scale > 1, "thumbnails are upscaled"


def test_animation_frames_are_spread_not_the_first_few():
    data = _animation([GREEN] * 8 + [RED] * 8, "GIF")
    frames = vision.animation_frames(data, max_frames=4)
    assert 2 <= len(frames) <= 4
    reds = 0
    for chunk in frames:
        with Image.open(io.BytesIO(chunk)) as im:
            r, g, b = im.convert("RGB").resize((1, 1)).getpixel((0, 0))
            reds += r > 150
    # Pillow merges identical consecutive frames on save, so the animation
    # has two frames; the samples must still reach the second half.
    assert reds >= 1, "the second half of the animation was never sampled"


def test_combine_is_shared_with_video():
    from kosherd import videocheck

    assert videocheck.combine is vision.combine
    assert vision.combine([ImageVerdict(CLEAN, ()), ImageVerdict(NSFW, ((1, 2, 3, 4),), True)]) \
        == ImageVerdict(NSFW, (), True)
