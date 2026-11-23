"""What one picture check costs in memory and time (#322).

A photo tens of megapixels in size was decoded at full size for each model,
and a page of them took the filter past 3 GB. What reaches the models is
now reduced first, and every box still lands on the original picture. The
nudity model runs on two threads per picture, as the person models do."""
import io

import pytest

Image = pytest.importorskip("PIL.Image")

from kosherd import persons, vision


def _picture(size, fmt):
    out = io.BytesIO()
    Image.new("RGB", size, (120, 140, 160)).save(out, fmt)
    return out.getvalue()


@pytest.mark.parametrize("fmt", ["JPEG", "PNG"])
def test_a_large_picture_is_reduced_for_the_detector(fmt):
    prepared, scale = vision.prepare(_picture((4000, 3000), fmt))
    assert scale == pytest.approx(vision.PREPARE_MAX_SIDE / 4000)
    with Image.open(io.BytesIO(prepared)) as image:
        assert image.size == (1600, 1200)


def test_a_picture_within_the_limit_goes_through_untouched():
    original = _picture((1600, 900), "JPEG")
    assert vision.prepare(original) == (original, 1.0)


def test_boxes_on_a_reduced_picture_land_on_the_original():
    # The detector answers in the reduced picture's pixels; the caller
    # divides by the scale, as it already did for enlarged thumbnails.
    seen = {}

    class Detector:
        def detect(self, path):
            with Image.open(path) as image:
                seen["size"] = image.size
            return [{"class": "FACE_FEMALE", "score": 0.9, "box": [800, 400, 160, 120]}]

    checker = vision.NudeNetDetector()
    checker._detector = Detector()
    found = checker.detect(_picture((4000, 3000), "JPEG"))
    assert seen["size"] == (1600, 1200)
    assert found[0].box == (2000, 1000, 400, 300)


class _Session:
    def __init__(self):
        self.tensors = []

    def run(self, _outputs, feed):
        self.tensors.append(next(iter(feed.values())))
        import numpy as np
        return [np.zeros((1, 1, 1), dtype=np.float32)]


def test_the_person_detector_reads_a_large_jpeg_reduced_but_answers_in_its_pixels(monkeypatch):
    pytest.importorskip("numpy")
    detector = persons.PersonDetector()
    detector._session, detector._input = _Session(), "images"
    calls = []
    monkeypatch.setattr(persons, "decode", lambda raw: ["box"])
    monkeypatch.setattr(persons, "people", lambda found, ratio, w, h, threshold:
                        calls.append((ratio, w, h)) or [])
    detector.detect(_picture((4000, 3000), "JPEG"))
    ratio, width, height = calls[0]
    assert (width, height) == (4000, 3000)
    # Whatever size the JPEG was decoded at, the ratio is the original's.
    assert ratio == pytest.approx(persons.INPUT / 4000, rel=0.01)
    assert detector._session.tensors[0].shape == (1, 3, persons.INPUT, persons.INPUT)


def test_the_nudity_model_runs_on_two_threads_per_picture(monkeypatch):
    ort = pytest.importorskip("onnxruntime")
    made = {}

    class Session:
        _model_path = "/models/320n.onnx"

    class Fake:
        onnx_session = Session()

    monkeypatch.setattr(ort, "InferenceSession",
                        lambda path, sess_options, providers: made.update(
                            path=path, threads=sess_options.intra_op_num_threads) or "rebuilt")
    detector = Fake()
    vision._limit_threads(detector)
    assert made == {"path": "/models/320n.onnx", "threads": vision.MODEL_THREADS}
    assert detector.onnx_session == "rebuilt"


def test_a_session_that_cannot_be_rebuilt_is_kept():
    class Fake:
        onnx_session = object()   # no model path to rebuild from

    detector = Fake()
    kept = detector.onnx_session
    vision._limit_threads(detector)
    assert detector.onnx_session is kept


def _photo(size=(640, 480)):
    # A noisy, skin-toned and blue picture: varied enough that a change in
    # how pixels are sampled or converted would change the fraction.
    import random

    rng = random.Random(7)
    image = Image.new("RGB", size)
    image.putdata([(200 + rng.randint(-30, 30), 150 + rng.randint(-30, 30), 120)
                   if (i % size[0]) < size[0] // 2 else (40, 60, 200 + rng.randint(-40, 40))
                   for i in range(size[0] * size[1])])
    out = io.BytesIO()
    image.save(out, "PNG")
    return out.getvalue()


def _old_skin_fraction(image_bytes, box):
    # How skin_fraction sampled before #322: convert all, crop, then sample.
    with Image.open(io.BytesIO(image_bytes)) as im:
        x, y, w, h = box
        right, bottom = min(x + w, im.width), min(y + h, im.height)
        region = im.convert("RGB").convert("YCbCr").crop((x, y, right, bottom))
        sample = region.resize((min(96, region.width), min(96, region.height)),
                               Image.Resampling.NEAREST)
        data = list(sample.getdata())
        return sum(1 for (Y, cb, cr) in data
                   if Y > 60 and 77 <= cb <= 127 and 133 <= cr <= 173) / len(data)


@pytest.mark.parametrize("box", [(0, 0, 640, 480), (100, 50, 300, 200), (300, 0, 40, 480)])
def test_skin_is_measured_on_exactly_the_same_pixels(box):
    photo = _photo()
    try:
        assert vision.skin_fraction(photo, box) == _old_skin_fraction(photo, box)
    finally:
        vision.forget_decoded()


def test_a_picture_measured_several_times_is_decoded_once(monkeypatch):
    photo, opened = _photo(), []
    real_open = Image.open
    monkeypatch.setattr(Image, "open", lambda *a, **k: opened.append(1) or real_open(*a, **k))
    try:
        for box in [(0, 0, 640, 480), (0, 0, 320, 240), (320, 240, 320, 240)]:
            vision.skin_fraction(photo, box)
        vision.face_lightness(photo, (10, 10, 100, 100))
        assert len(opened) == 1
    finally:
        vision.forget_decoded()


def test_the_decoded_picture_is_let_go_when_the_check_ends(tmp_path):
    class Detector:
        available = True

        def detect(self, image_bytes):
            vision.skin_fraction(image_bytes, (0, 0, 64, 64))   # decodes and keeps it
            return []

    checker = vision.ImageFilter(detector=Detector(), cache=vision.VerdictCache(tmp_path / "v.sqlite"))
    checker.judge_bytes(_photo())
    assert getattr(vision._decoded, "entry", None) is None
