"""Modern CDN images must take the complete image-analysis path."""

import io

import pytest

from kosherd import imageedit, vision


@pytest.fixture
def avif():
    Image = pytest.importorskip("PIL.Image")
    features = pytest.importorskip("PIL.features")
    if not features.check("avif"):
        pytest.skip("this test environment's Pillow has no AVIF codec")
    out = io.BytesIO()
    Image.new("RGB", (320, 240), (100, 140, 180)).save(out, "AVIF", quality=80)
    return out.getvalue()


def test_avif_is_resized_and_converted_for_the_base_detector(avif):
    from PIL import Image

    prepared, scale = vision.prepare(avif)
    assert scale == pytest.approx(640 / 240)
    with Image.open(io.BytesIO(prepared)) as image:
        assert image.format == "JPEG"
        assert image.size == (int(320 * scale), int(240 * scale))


def test_avif_receives_person_refinement_not_only_the_base_verdict(avif, monkeypatch):
    measured = []
    face = vision.Detection("FACE_FEMALE", 0.95, (100, 20, 40, 40))

    def too_much(data, body, reference=None, **kwargs):
        measured.append(body)
        return True

    monkeypatch.setattr(vision, "shows_too_much", too_much)
    verdict = vision.ImageFilter._person_aware(avif, [face], vision.judge([face]))
    assert measured, "a supported AVIF must not stop at the base model"
    assert verdict.level == vision.IMMODEST
    assert verdict.has_person


def test_avif_can_be_covered_and_replaced_without_losing_dimensions(avif):
    from PIL import Image

    covered = imageedit.cover(avif, [(100, 80, 40, 60)])
    tile = imageedit.placeholder_for(avif)
    for result in (covered, tile):
        assert result is not None
        with Image.open(io.BytesIO(result)) as image:
            image.load()
            assert image.size == (320, 240)


def test_missing_refinement_decoder_cannot_cache_a_partial_clean_verdict(tmp_path, monkeypatch):
    Image = pytest.importorskip("PIL.Image")
    # OpenCV and Pillow do not necessarily have the same codecs. A base
    # model returning CLEAN does not prove the skin/person path could run.
    class BaseDetector:
        def detect(self, data):
            return []

    def unsupported(*args, **kwargs):
        raise Image.UnidentifiedImageError("decoder unavailable")

    monkeypatch.setattr(Image, "open", unsupported)
    cache = vision.VerdictCache(tmp_path / "cache.sqlite")
    filt = vision.ImageFilter(detector=BaseDetector(), cache=cache)
    body = b"unsupported image" * 300
    assert filt.judge_bytes(body) is None
    assert cache.get(vision.digest(body)) is None
