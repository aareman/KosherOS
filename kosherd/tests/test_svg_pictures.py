"""SVG pictures are drawn and judged, not hidden unread (#322).

Neither the model nor Pillow reads SVG, so every SVG icon and logo came back
"could not be judged" and was hidden for accounts with picture checking."""
import io

import pytest

from kosherd import vision

Image = pytest.importorskip("PIL.Image")

ICON = (b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 12">'
        b'<rect x="0" y="0" width="12" height="12" fill="#c00"/></svg>')


def _drawable():
    try:
        import gi

        gi.require_version("Rsvg", "2.0")
        gi.require_foreign("cairo")
        from gi.repository import Rsvg  # noqa: F401
    except Exception:  # noqa: BLE001
        pytest.skip("librsvg is not available here")


def test_an_svg_is_recognised_by_its_first_bytes():
    assert vision.looks_svg(ICON)
    assert vision.looks_svg(b'\n<?xml version="1.0"?>\n' + ICON)
    assert not vision.looks_svg(b"\x89PNG\r\n\x1a\n" + b"<svg" * 4)
    assert not vision.looks_svg(b"<html><body>no drawing</body></html>")


def test_an_icon_is_drawn_on_white_at_a_size_the_model_can_read():
    _drawable()
    with Image.open(io.BytesIO(vision.render_svg(ICON))) as drawn:
        assert drawn.size == (256, 128)          # the viewBox's shape, enlarged
        rgb = drawn.convert("RGB")
        assert rgb.getpixel((60, 60))[0] > 180 and rgb.getpixel((60, 60))[1] < 60
        assert rgb.getpixel((200, 60)) == (255, 255, 255)


def test_a_drawing_that_cannot_be_drawn_is_not_judged():
    _drawable()
    assert vision.render_svg(b"<svg><unclosed") is None
    assert vision.render_svg(b"<svg " + b" " * vision.SVG_MAX_BYTES + b"/>") is None


def test_a_drawing_reads_no_file_it_points_at(tmp_path):
    _drawable()
    picture = tmp_path / "p.png"
    Image.new("RGB", (40, 40), (0, 0, 0)).save(picture)
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" '
           f'xmlns:xlink="http://www.w3.org/1999/xlink" viewBox="0 0 40 40">'
           f'<image href="file://{picture}" xlink:href="file://{picture}" '
           f'x="0" y="0" width="40" height="40"/></svg>').encode()
    with Image.open(io.BytesIO(vision.render_svg(svg))) as drawn:
        assert drawn.convert("RGB").getpixel((128, 128)) == (255, 255, 255)


def test_the_model_judges_the_drawing_and_the_verdict_is_kept_for_the_svg(tmp_path):
    _drawable()
    seen = []

    class Detector:
        available = True

        def detect(self, image_bytes):
            seen.append(image_bytes[:8])
            return []

    checker = vision.ImageFilter(detector=Detector(),
                                 cache=vision.VerdictCache(tmp_path / "v.sqlite"))
    svg = ICON + b"<!--" + b" " * vision.MIN_IMAGE_BYTES + b"-->"
    verdict = checker.judge_bytes(svg)
    assert verdict is not None and verdict.level == vision.CLEAN
    assert seen == [b"\x89PNG\r\n\x1a\n"]
    assert checker.cache.get(vision.digest(svg)) == verdict


def test_an_svg_that_cannot_be_drawn_is_still_not_judged(tmp_path):
    class Detector:
        available = True

        def detect(self, image_bytes):
            raise AssertionError("never asked")

    checker = vision.ImageFilter(detector=Detector(),
                                 cache=vision.VerdictCache(tmp_path / "v.sqlite"))
    assert checker.judge_bytes(b"<svg><unclosed" + b" " * vision.MIN_IMAGE_BYTES) is None
