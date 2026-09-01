"""Looking at pictures: the mapping, the caching, and what happens when
the model is not there.

The model itself is not exercised here — it is a 5 MB ONNX file that the
test environment does not have, and pinning a test to its exact scores
would test the model rather than our use of it. What is tested is
everything around it, and in particular the two failure directions: a
picture that cannot be judged must not be shown, and a picture that is
merely small must not cost inference.
"""

import pytest

from kosherd import imageedit, vision
from kosherd.vision import CLEAN, IMMODEST, NSFW, SUGGESTIVE, Detection


def det(label, score=0.9, box=(10, 20, 30, 40)):
    return Detection(label=label, score=score, box=box)


# -- the ladder ---------------------------------------------------------------

def test_exposure_is_explicit():
    assert vision.level_of([det("FEMALE_BREAST_EXPOSED")]) == NSFW
    assert vision.level_of([det("MALE_GENITALIA_EXPOSED")]) == NSFW


def test_covered_but_prominent_is_suggestive():
    assert vision.level_of([det("FEMALE_BREAST_COVERED")]) == SUGGESTIVE
    assert vision.level_of([det("BELLY_EXPOSED")]) == SUGGESTIVE


def test_the_weakest_signals_are_immodest():
    assert vision.level_of([det("FEET_EXPOSED")]) == IMMODEST
    assert vision.level_of([det("BELLY_COVERED")]) == IMMODEST


def test_a_face_alone_is_not_a_finding():
    # Otherwise every photograph of a person is a finding, which is not a
    # filter, it is an off switch with extra steps.
    assert vision.level_of([det("FEMALE_FACE"), det("MALE_FACE")]) == CLEAN


def test_the_strongest_finding_wins():
    assert vision.level_of([det("FEET_EXPOSED"),
                            det("FEMALE_BREAST_EXPOSED")]) == NSFW


def test_a_low_confidence_guess_is_not_a_finding():
    assert vision.level_of([det("FEMALE_BREAST_EXPOSED", score=0.05)]) == CLEAN


def test_the_verdict_carries_the_regions_worth_covering():
    verdict = vision.judge([det("FEMALE_BREAST_EXPOSED", box=(1, 2, 3, 4)),
                            det("FEMALE_FACE", box=(5, 6, 7, 8))])
    assert verdict.level == NSFW
    # The face is not the problem and covering it helps nobody.
    assert verdict.regions == ((1, 2, 3, 4),)


# -- what each setting hides --------------------------------------------------

@pytest.mark.parametrize("level,expect_hidden", [
    ("none", []),
    ("nsfw", [NSFW]),
    ("suggestive", [NSFW, SUGGESTIVE]),
    ("immodest", [NSFW, SUGGESTIVE, IMMODEST]),
    ("all", [NSFW, SUGGESTIVE, IMMODEST, CLEAN]),
])
def test_each_media_level_hides_what_it_says(level, expect_hidden):
    for found in (CLEAN, IMMODEST, SUGGESTIVE, NSFW):
        verdict = vision.ImageVerdict(found, ())
        assert vision.hides(level, verdict) == (found in expect_hidden), \
            f"{level} vs {found}"


def test_hide_everything_needs_no_detector_at_all():
    # Which is exactly why it is the setting to fall back to.
    assert vision.hides("all", vision.ImageVerdict(CLEAN, ()))


# -- caching ------------------------------------------------------------------

def test_the_same_picture_is_judged_once(tmp_path):
    calls = []

    class Once:
        available = True

        def detect(self, data):
            calls.append(data)
            return [det("FEMALE_BREAST_EXPOSED")]

    f = vision.ImageFilter(detector=Once(),
                           cache=vision.VerdictCache(tmp_path / "i.sqlite"))
    blob = b"x" * 10_000
    assert f.verdict(blob).level == NSFW
    assert f.verdict(blob).level == NSFW
    assert len(calls) == 1


def test_regions_survive_the_cache(tmp_path):
    cache = vision.VerdictCache(tmp_path / "i.sqlite")
    cache.put("abc", vision.ImageVerdict(NSFW, ((1, 2, 3, 4), (5, 6, 7, 8))))
    assert cache.get("abc").regions == ((1, 2, 3, 4), (5, 6, 7, 8))


def test_an_expired_verdict_is_judged_again(tmp_path):
    cache = vision.VerdictCache(tmp_path / "i.sqlite", ttl=-1)
    cache.put("abc", vision.ImageVerdict(NSFW, ()))
    assert cache.get("abc") is None


# -- failure directions -------------------------------------------------------

def test_a_tiny_picture_never_reaches_the_model(tmp_path):
    class Never:
        available = True

        def detect(self, data):
            raise AssertionError("should not be called for an icon")

    f = vision.ImageFilter(detector=Never(),
                           cache=vision.VerdictCache(tmp_path / "i.sqlite"))
    assert f.verdict(b"tiny").level == CLEAN


def test_no_model_means_unjudged_not_clean(tmp_path):
    # The distinction the caller depends on: "looked and found nothing"
    # must not be confused with "could not look".
    f = vision.ImageFilter(detector=vision.NullDetector(),
                           cache=vision.VerdictCache(tmp_path / "i.sqlite"))
    assert f.verdict(b"x" * 10_000) is None
    assert not f.available


def test_a_slow_picture_gives_up_rather_than_stalling_the_page(tmp_path):
    import time

    class Slow:
        available = True

        def detect(self, data):
            time.sleep(5)
            return []

    f = vision.ImageFilter(detector=Slow(),
                           cache=vision.VerdictCache(tmp_path / "i.sqlite"),
                           timeout=0.1)
    started = time.monotonic()
    assert f.verdict(b"x" * 10_000) is None
    assert time.monotonic() - started < 2


def test_a_detector_that_throws_is_not_a_clean_verdict(tmp_path):
    class Broken:
        available = True

        def detect(self, data):
            raise RuntimeError("model exploded")

    f = vision.ImageFilter(detector=Broken(),
                           cache=vision.VerdictCache(tmp_path / "i.sqlite"))
    assert f.verdict(b"x" * 10_000) is None


# -- covering regions ---------------------------------------------------------

@pytest.fixture
def photo():
    Image = pytest.importorskip("PIL.Image")
    import io

    image = Image.new("RGB", (200, 200), (10, 200, 10))
    # A fine checkerboard inside the region: a flat colour would pixelate
    # to the same flat colour and prove nothing.
    for x in range(40, 120):
        for y in range(40, 120):
            image.putpixel((x, y), (255, 0, 0) if (x + y) % 2 else (0, 0, 255))
    out = io.BytesIO()
    image.save(out, "PNG")
    return out.getvalue()


def test_covering_changes_the_region_and_leaves_the_rest(photo):
    from PIL import Image
    import io

    covered = imageedit.cover(photo, [(40, 40, 80, 80)])
    assert covered is not None
    after = Image.open(io.BytesIO(covered)).convert("RGB")
    # Outside the region (with its margin), the picture is untouched.
    assert after.getpixel((5, 5)) == (10, 200, 10)
    # Inside, the fine detail is gone: the checkerboard has averaged into
    # a handful of flat blocks, so nothing of it is recoverable.
    patch = {after.getpixel((x, y)) for x in range(50, 110, 2)
             for y in range(50, 110, 2)}
    assert patch & {(255, 0, 0), (0, 0, 255)} == set()
    assert len(patch) < 40


def test_covering_nothing_is_not_an_edit(photo):
    assert imageedit.cover(photo, []) is None


def test_an_unreadable_picture_reports_failure_rather_than_returning_it():
    # Returning the original would be the one failure mode that matters.
    assert imageedit.cover(b"not an image at all", [(0, 0, 10, 10)]) is None


# -- keeping up with the machine ----------------------------------------------

def _slow_filter(tmp_path, ms, slow_ms=400):
    import time as _t

    class Slow:
        available = True

        def detect(self, data):
            _t.sleep(ms / 1000)
            return []

    return vision.ImageFilter(detector=Slow(),
                              cache=vision.VerdictCache(tmp_path / "i.sqlite"),
                              slow_ms=slow_ms)


def test_a_machine_that_keeps_up_keeps_checking(tmp_path):
    f = _slow_filter(tmp_path, ms=5)
    for i in range(vision.SLOW_WINDOW + 4):
        assert f.verdict(b"x" * 10_000 + bytes([i % 251])) is not None
    assert not f.degraded


def test_a_machine_that_cannot_keep_up_stops_trying(tmp_path):
    # Measured on two cores: 200-600 ms per image. A news page with thirty
    # photographs is then fifteen seconds of waiting for a half-checked
    # page — worse for the person than the setting that needs no model.
    f = _slow_filter(tmp_path, ms=60, slow_ms=20)
    for i in range(vision.SLOW_WINDOW):
        f.verdict(b"x" * 10_000 + bytes([i % 251]))
    assert f.degraded
    # And from then on it answers immediately with "could not judge",
    # which the caller turns into a hidden picture.
    import time as _t
    started = _t.monotonic()
    assert f.verdict(b"y" * 10_000) is None
    assert _t.monotonic() - started < 0.02


def test_a_verdict_already_reached_is_still_served_when_degraded(tmp_path):
    # It costs nothing and is exactly as accurate on a slow machine.
    f = _slow_filter(tmp_path, ms=60, slow_ms=20)
    blob = b"z" * 10_000
    first = f.verdict(blob)
    for i in range(vision.SLOW_WINDOW):
        f.verdict(b"x" * 10_000 + bytes([i % 251]))
    assert f.degraded
    assert f.verdict(blob) == first


def test_one_slow_picture_does_not_condemn_the_machine(tmp_path):
    f = _slow_filter(tmp_path, ms=1, slow_ms=20)
    for i in range(vision.SLOW_WINDOW):
        f.verdict(b"x" * 10_000 + bytes([i % 251]))
    assert not f.degraded


# -- the picture judged by the page it is on ----------------------------------

def test_a_person_on_an_immodest_page_is_hidden():
    # The gap this closes: a clothed model in a lingerie catalogue has no
    # exposure labels, so the detector calls the picture clean and it
    # stays on screen next to the word "lingerie".
    clothed_model = vision.ImageVerdict(CLEAN, (), has_person=True)
    assert vision.in_context(clothed_model, page_level=IMMODEST,
                             tolerance=IMMODEST)


def test_a_logo_on_the_same_page_is_not():
    # Hiding every picture on the page would take out the shop's own
    # navigation, which is how a filter becomes an outage.
    logo = vision.ImageVerdict(CLEAN, (), has_person=False)
    assert not vision.in_context(logo, page_level=IMMODEST, tolerance=IMMODEST)


def test_a_person_on_an_ordinary_page_is_not():
    # Otherwise every news photograph goes, everywhere.
    photo = vision.ImageVerdict(CLEAN, (), has_person=True)
    assert not vision.in_context(photo, page_level="", tolerance=IMMODEST)
    assert not vision.in_context(photo, page_level=CLEAN, tolerance=IMMODEST)


def test_the_page_has_to_be_bad_enough_for_this_account():
    photo = vision.ImageVerdict(CLEAN, (), has_person=True)
    # An account that only hides explicit pictures is not asking for this.
    assert not vision.in_context(photo, page_level=IMMODEST, tolerance=NSFW)
    assert vision.in_context(photo, page_level=NSFW, tolerance=NSFW)


def test_a_face_is_enough_to_count_as_a_person():
    # The detector is reliable about finding people and unreliable about
    # judging modesty, which is exactly why the two are used differently.
    assert vision.judge([det("FEMALE_FACE")]).has_person
    assert vision.judge([det("MALE_FACE")]).has_person
    assert not vision.judge([]).has_person
    # ...and a face alone is still not a finding on its own.
    assert vision.judge([det("FEMALE_FACE")]).level == CLEAN


def test_whether_there_was_a_person_survives_the_cache(tmp_path):
    cache = vision.VerdictCache(tmp_path / "i.sqlite")
    cache.put("abc", vision.ImageVerdict(CLEAN, (), has_person=True))
    assert cache.get("abc").has_person is True
    cache.put("def", vision.ImageVerdict(CLEAN, (), has_person=False))
    assert cache.get("def").has_person is False
