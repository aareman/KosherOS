"""The cover: what it destroys, what it keeps, and what it costs.

Three rounds of family feedback shaped it (see imageedit.py); the fourth
was "blur is not so effective in blocking images". A blur at any radius
that keeps a page fast leaves a figure's silhouette and skin tone readable.
The cover now shrinks the region to a handful of cells before smoothing it
back, which throws the shape away entirely and is cheaper than the blur it
replaces.
"""

import io

import pytest

from kosherd import imageedit

Image = pytest.importorskip("PIL.Image")


def _photo(fmt="PNG", size=400):
    image = Image.new("RGB", (size, size), (10, 200, 10))
    # A high-contrast figure: a dark silhouette with a bright edge, the
    # kind of shape a plain blur leaves recognisable.
    for x in range(size // 4, size // 2):
        for y in range(size // 4, 3 * size // 4):
            image.putpixel((x, y), (20, 20, 20) if x < size * 3 // 8 else (240, 220, 200))
    out = io.BytesIO()
    image.save(out, fmt)
    return out.getvalue()


def test_the_cover_erases_the_shape_not_just_the_detail():
    src = _photo()
    region = (100, 100, 100, 200)
    covered = Image.open(io.BytesIO(imageedit.cover(src, [region]))).convert("RGB")
    # Inside the figure, the sharp edge between dark and bright is gone:
    # neighbouring pixels across where the edge was differ by little.
    y = 200
    edge = 150
    left = covered.getpixel((edge - 6, y))
    right = covered.getpixel((edge + 6, y))
    assert max(abs(a - b) for a, b in zip(left, right)) < 40, (left, right)
    # And nothing near the original extremes survives inside the region.
    pixels = [covered.getpixel((x, yy)) for x in range(110, 190, 4) for yy in range(110, 290, 4)]
    assert not any(max(p) < 45 for p in pixels), "the dark silhouette is still there"
    assert not any(p[0] > 225 and p[1] > 205 for p in pixels), "the bright edge is still there"


def test_far_outside_the_region_nothing_changes():
    src = _photo()
    covered = Image.open(io.BytesIO(imageedit.cover(src, [(100, 100, 100, 200)]))).convert("RGB")
    assert covered.getpixel((395, 395)) == (10, 200, 10)
    assert covered.getpixel((5, 5)) == (10, 200, 10)


def test_the_margin_is_generous():
    # Three rounds of "the covered area is too small".
    assert imageedit.MARGIN >= 0.5


def test_each_format_comes_back_as_itself_where_that_is_cheap():
    jpeg = imageedit.cover(_photo("JPEG"), [(100, 100, 50, 50)])
    assert jpeg[:2] == b"\xff\xd8" and imageedit.content_type(jpeg) == "image/jpeg"
    webp = imageedit.cover(_photo("WEBP"), [(100, 100, 50, 50)])
    assert webp[:4] == b"RIFF" and imageedit.content_type(webp) == "image/webp"
    png = imageedit.cover(_photo("PNG"), [(100, 100, 50, 50)])
    assert png[:4] == b"\x89PNG" and imageedit.content_type(png) == "image/png"


def test_webp_stays_small():
    # It came back as PNG before: five to ten times the bytes for a photo.
    src = _photo("WEBP")
    out = imageedit.cover(src, [(100, 100, 50, 50)])
    assert len(out) < len(imageedit.cover(_photo("PNG"), [(100, 100, 50, 50)]))


def test_dominant_reads_the_header_and_answers_by_area():
    src = _photo()
    assert imageedit.dominant(src, [(20, 20, 360, 360)])
    assert not imageedit.dominant(src, [(100, 100, 40, 40)])
    assert not imageedit.dominant(src, [])
    assert not imageedit.dominant(b"not a picture", [(0, 0, 10, 10)])


def test_dominant_counts_overlapping_boxes_once():
    src = _photo()
    # Two copies of the same small box are still one small box.
    assert not imageedit.dominant(src, [(100, 100, 40, 40)] * 20)


def test_the_cover_cache_is_bounded_and_recent_first():
    cache = imageedit.CoverCache(max_bytes=1000)
    for name in "abcd":
        cache.put(name, name.encode() * 240)
    assert cache.get("a") == b"a" * 240  # touched: now most recent
    cache.put("e", b"e" * 240)           # over the bound: the least recent (b) goes
    assert cache.get("b") is None
    assert cache.get("a") is not None and cache.get("e") is not None
    cache.put("huge", b"h" * 300)        # more than a quarter of the cache: not kept
    assert cache.get("huge") is None


def test_the_cover_is_not_slow():
    import time

    src = _photo(size=1200)
    started = time.monotonic()
    imageedit.cover(src, [(200, 200, 500, 700)])
    assert time.monotonic() - started < 1.5, "a cover must not cost more than a detection"
