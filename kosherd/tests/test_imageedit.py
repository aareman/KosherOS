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


def test_the_frost_is_neutral_not_skin_toned():
    # A figure's average colour is skin tone, and a cover pulled toward it
    # read as a pink blob. The cover must come out grey: low saturation
    # everywhere inside the region, whatever colour the region was.
    im = Image.new("RGB", (400, 400), (10, 200, 10))
    for x in range(100, 300):
        for y in range(80, 360):
            im.putpixel((x, y), (230, 160, 140))  # a warm pink skin tone
    out = io.BytesIO(); im.save(out, "PNG")
    covered = Image.open(io.BytesIO(imageedit.cover(out.getvalue(), [(120, 100, 160, 240)]))).convert("RGB")
    for x in range(150, 250, 10):
        for y in range(150, 300, 10):
            r, g, b = covered.getpixel((x, y))
            assert max(r, g, b) - min(r, g, b) < 24, (x, y, (r, g, b))


def test_the_covers_edge_is_a_few_pixels_not_a_band_the_figure_shows_through():
    # A 40px feather let the face and shins through at the top and bottom
    # of a figure, and blended a ring of skin tone round the grey that read
    # as a pink box. Now only the outermost pixels soften.
    import io

    from PIL import Image, ImageDraw

    im = Image.new("RGB", (300, 600), (214, 196, 170))
    ImageDraw.Draw(im).rectangle((100, 0, 200, 600), fill=(224, 172, 130))  # skin column
    out = io.BytesIO(); im.save(out, "JPEG", quality=92)
    covered = imageedit.cover(out.getvalue(), [(100, 40, 100, 520)])
    with Image.open(io.BytesIO(covered)) as result:
        # The grown region reaches the top and bottom of the picture; a
        # pixel just inside its edge, over the skin column, must be grey,
        # not skin.
        for y in (12, 300, 588):
            r, g, b = result.getpixel((150, y))
            assert max(r, g, b) - min(r, g, b) < 24, (y, (r, g, b))


def test_a_placeholder_keeps_size_and_only_a_flat_approximate_color():
    import io

    from PIL import Image

    out = io.BytesIO()
    Image.new("RGB", (123, 45), (200, 30, 30)).save(out, "PNG")
    tile = imageedit.placeholder_for(out.getvalue())
    with Image.open(io.BytesIO(tile)) as im:
        assert im.size == (123, 45)
        rgb = im.convert("RGB")
        color = rgb.getpixel((0, 0))
        assert all(low == high for low, high in rgb.getextrema()), "no spatial detail"
        assert all(abs(got - wanted) <= 32 for got, wanted in zip(color, (200, 30, 30)))
        assert max(color) - min(color) < 170, "slightly mute the source saturation"
    assert imageedit.placeholder_for(b"not a picture") is None
    assert imageedit.placeholder(123, 45, color) is tile, "cached by size and color"


def test_placeholder_color_is_whole_image_average_not_a_sampled_detail():
    image = Image.new("RGB", (128, 64), (240, 0, 0))
    image.paste((0, 0, 240), (64, 0, 128, 64))
    out = io.BytesIO()
    image.save(out, "PNG")
    with Image.open(io.BytesIO(imageedit.placeholder_for(out.getvalue()))) as tile:
        rgb = tile.convert("RGB")
        assert rgb.size == image.size
        assert all(low == high for low, high in rgb.getextrema())
        color = rgb.getpixel((0, 0))
        assert all(abs(got - wanted) <= 8 for got, wanted in zip(color, (120, 0, 120)))


def test_same_size_different_colors_do_not_share_a_placeholder():
    tiles = []
    for color in ((240, 0, 0), (0, 0, 240)):
        out = io.BytesIO()
        Image.new("RGB", (120, 80), color).save(out, "PNG")
        tiles.append(imageedit.placeholder_for(out.getvalue()))
    assert tiles[0] != tiles[1]


def test_placeholder_transparency_does_not_reveal_invisible_pixel_color():
    out = io.BytesIO()
    Image.new("RGBA", (40, 30), (255, 0, 0, 0)).save(out, "PNG")
    with Image.open(io.BytesIO(imageedit.placeholder_for(out.getvalue()))) as tile:
        red, green, blue = tile.convert("RGB").getpixel((0, 0))
        assert red == green == blue
        assert abs(red - imageedit.PLACEHOLDER_GREY[0]) <= 8


def test_undecodable_pixels_keep_dimensions_with_neutral_fallback(monkeypatch):
    out = io.BytesIO()
    Image.new("RGB", (120, 80), (200, 30, 30)).save(out, "PNG")

    def broken(_):
        raise ValueError("truncated pixels")

    imageedit._placeholder_sources.clear()
    monkeypatch.setattr(imageedit, "_placeholder_color", broken)
    with Image.open(io.BytesIO(imageedit.placeholder_for(out.getvalue()))) as tile:
        assert tile.size == (120, 80)
        assert tile.convert("RGB").getpixel((0, 0)) == imageedit.PLACEHOLDER_GREY


def test_cover_cache_hit_survives_concurrent_eviction_and_keeps_accounting():
    from collections import OrderedDict
    from concurrent.futures import ThreadPoolExecutor
    import threading

    cache = imageedit.CoverCache(max_bytes=16)
    for key in ("first", "b", "c", "d"):
        cache.put(key, b"1234")
    read, release, writing, written = (threading.Event() for _ in range(4))

    class PausingLookup(OrderedDict):
        def get(self, key):
            value = super().get(key)
            if key == "first" and not read.is_set():
                read.set()
                assert release.wait(2)
            return value

    cache._items = PausingLookup(cache._items)

    def evict():
        writing.set()
        cache.put("new", b"5678")
        written.set()

    with ThreadPoolExecutor(max_workers=2) as pool:
        hit = pool.submit(cache.get, "first")
        try:
            assert read.wait(2)
            eviction = pool.submit(evict)
            assert writing.wait(2)
            # Without synchronization the insertion can evict "first"
            # between lookup and LRU promotion, making the cache hit fail.
            written.wait(0.05)
        finally:
            release.set()
        assert hit.result(timeout=2) == b"1234"
        eviction.result(timeout=2)
        assert cache.get("first") == b"1234"
        assert cache.get("b") is None

        def replace(worker):
            for i in range(100):
                key = f"key-{i % 5}"
                cache.put(key, bytes([worker]) * (1 + i % 4))
                cache.get(key)
                assert len(cache) <= 16
        list(pool.map(replace, range(4)))

    assert cache._size == sum(map(len, cache._items.values()))
    assert 0 <= cache._size <= cache.max_bytes


def test_placeholder_reuses_the_color_without_redecoding_pixels(monkeypatch):
    imageedit._placeholder_sources.clear()
    out = io.BytesIO()
    Image.new("RGB", (120, 80), (40, 120, 200)).save(out, "PNG")
    original = imageedit._placeholder_color
    calls = []

    def sample(image):
        calls.append(image.size)
        return original(image)

    monkeypatch.setattr(imageedit, "_placeholder_color", sample)
    first = imageedit.placeholder_for(out.getvalue())
    assert imageedit.placeholder_for(out.getvalue()) is first
    assert calls == [(120, 80)]
