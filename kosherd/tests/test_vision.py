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
    assert vision.level_of([det("FACE_FEMALE"), det("FACE_MALE")]) == CLEAN


def test_the_strongest_finding_wins():
    assert vision.level_of([det("FEET_EXPOSED"),
                            det("FEMALE_BREAST_EXPOSED")]) == NSFW


def test_a_low_confidence_guess_is_not_a_finding():
    assert vision.level_of([det("FEMALE_BREAST_EXPOSED", score=0.05)]) == CLEAN


def test_the_verdict_carries_the_regions_worth_covering():
    verdict = vision.judge([det("FEMALE_BREAST_EXPOSED", box=(1, 2, 3, 4)),
                            det("FACE_FEMALE", box=(5, 6, 7, 8))])
    assert verdict.level == NSFW
    # The face is not the problem and covering it helps nobody.
    assert verdict.regions == ((1, 2, 3, 4),)


# -- what each setting hides --------------------------------------------------

@pytest.mark.parametrize("level,expect_hidden", [
    ("none", []),
    ("nsfw", [NSFW]),
    ("suggestive", [NSFW, SUGGESTIVE]),
    ("immodest", [NSFW, SUGGESTIVE, IMMODEST]),
    ("people", [NSFW, SUGGESTIVE, IMMODEST]),
    ("all", [NSFW, SUGGESTIVE, IMMODEST, CLEAN]),
])
def test_each_media_level_hides_what_it_says(level, expect_hidden):
    for found in (CLEAN, IMMODEST, SUGGESTIVE, NSFW):
        verdict = vision.ImageVerdict(found, ())
        assert vision.hides(level, verdict) == (found in expect_hidden), \
            f"{level} vs {found}"


def test_the_people_level_hides_anyone_whatever_they_wear():
    # "sensual tight and/or transparent clothing gets through like comic
    # book super women": no skin to measure, no label to fire. The level
    # above immodest asks only whether there is a person.
    clothed = vision.ImageVerdict(CLEAN, (), has_person=True)
    assert vision.hides("people", clothed)
    assert not vision.hides("immodest", clothed)
    landscape = vision.ImageVerdict(CLEAN, (), has_person=False)
    assert not vision.hides("people", landscape)
    from kosherd.policy import MEDIA_LEVELS

    assert MEDIA_LEVELS.index("immodest") < MEDIA_LEVELS.index("people") < MEDIA_LEVELS.index("all")


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

    covered = imageedit.cover(photo, [(40, 40, 60, 60)])
    assert covered is not None
    after = Image.open(io.BytesIO(covered)).convert("RGB")
    # Far outside the region and its (now generous) margin, untouched.
    assert after.getpixel((195, 195)) == (10, 200, 10)
    # Inside, the fine detail is gone. The cover is a heavy blur with
    # noise (not flat pixel blocks), so the test is not "few colours" but
    # "nothing of the original survives": no pure checkerboard pixel, and
    # nothing even close to the saturated red/blue it was made of.
    pixels = [after.getpixel((x, y)) for x in range(55, 95, 2)
              for y in range(55, 95, 2)]
    assert not any(p in ((255, 0, 0), (0, 0, 255)) for p in pixels)
    assert not any((r > 220 and g < 40 and b < 40)
                   or (b > 220 and r < 40 and g < 40)
                   for r, g, b in pixels), "saturated originals leaked through"


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
        calls = 0

        def detect(self, data):
            Slow.calls += 1
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
    # And from then on it answers "could not judge" without consulting the
    # detector at all, which the caller turns into a hidden picture.
    before = f.detector.calls
    assert f.verdict(b"y" * 10_000) is None
    assert f.detector.calls == before


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
    assert vision.judge([det("FACE_FEMALE")]).has_person
    assert vision.judge([det("FACE_MALE")]).has_person
    assert not vision.judge([]).has_person
    # ...and a face alone is still not a finding on its own.
    assert vision.judge([det("FACE_FEMALE")]).level == CLEAN


def test_whether_there_was_a_person_survives_the_cache(tmp_path):
    cache = vision.VerdictCache(tmp_path / "i.sqlite")
    cache.put("abc", vision.ImageVerdict(CLEAN, (), has_person=True))
    assert cache.get("abc").has_person is True
    cache.put("def", vision.ImageVerdict(CLEAN, (), has_person=False))
    assert cache.get("def").has_person is False


# -- skin-exposure promotion: what the labels cannot see -------------------------

def _skin_photo(skin_box=None, size=(300, 400)):
    """A photo with a skin-toned rectangle where a figure would be."""
    from PIL import Image, ImageDraw
    import io

    im = Image.new("RGB", size, (30, 90, 160))  # blue background
    if skin_box:
        ImageDraw.Draw(im).rectangle(skin_box, fill=(224, 172, 130))  # skin tone
    out = io.BytesIO(); im.save(out, "PNG")
    return out.getvalue()


def test_a_female_face_over_exposed_skin_is_promoted_to_immodest():
    # Bare shoulders / short skirt: no exposure class exists, so the labels
    # say clean. The body below the face is mostly skin — promote.
    photo = _skin_photo(skin_box=(60, 60, 240, 400))
    face = vision.Detection("FACE_FEMALE", 0.9, (120, 30, 60, 60))
    verdict = vision.ImageFilter._person_aware(photo, [face],
                                               vision.judge([face]))
    assert verdict.level == vision.IMMODEST
    assert verdict.regions, "the figure is what gets covered"


def test_a_female_face_over_clothing_stays_clean():
    photo = _skin_photo(skin_box=None)  # clothed: no skin below the face
    face = vision.Detection("FACE_FEMALE", 0.9, (120, 30, 60, 60))
    verdict = vision.ImageFilter._person_aware(photo, [face],
                                               vision.judge([face]))
    assert verdict.level == vision.CLEAN


def test_a_figure_from_behind_with_exposed_skin_is_promoted():
    # Photographed from the back: no face, only a weak covered-buttocks
    # detection — but the figure is mostly skin (legs, arms).
    photo = _skin_photo(skin_box=(80, 40, 220, 380))
    part = vision.Detection("BUTTOCKS_COVERED", 0.15, (120, 200, 60, 50))
    verdict = vision.ImageFilter._person_aware(photo, [part],
                                               vision.judge([part]))
    assert verdict.level == vision.IMMODEST


def test_a_figure_from_behind_fully_clothed_stays_clean():
    photo = _skin_photo(skin_box=None)
    part = vision.Detection("BUTTOCKS_COVERED", 0.15, (120, 200, 60, 50))
    verdict = vision.ImageFilter._person_aware(photo, [part],
                                               vision.judge([part]))
    assert verdict.level == vision.CLEAN


def test_bare_legs_under_a_short_skirt_are_caught_even_with_a_covered_top():
    # Over the whole figure the legs are a sixth of the box and the picture
    # used to pass; measured on their own they are most of the legs region.
    photo = _skin_photo(skin_box=(110, 230, 190, 400))
    face = vision.Detection("FACE_FEMALE", 0.9, (120, 30, 60, 60))
    body = vision.body_box(face.box, 300, 400)
    assert vision.skin_fraction(photo, body) < vision.SKIN_LIMIT, "the old measure misses it"
    verdict = vision.ImageFilter._person_aware(photo, [face], vision.judge([face]))
    assert verdict.level == vision.IMMODEST


def test_a_beige_floor_under_a_clothed_figure_is_not_legs():
    # Skin-toned only in the bottom strip, below where legs are measured.
    photo = _skin_photo(skin_box=(0, 350, 300, 400))
    face = vision.Detection("FACE_FEMALE", 0.9, (120, 30, 60, 60))
    verdict = vision.ImageFilter._person_aware(photo, [face], vision.judge([face]))
    assert verdict.level == vision.CLEAN


def _grey_photo(light_box=None, size=(300, 400), background=40, light=170,
                tint=None, mode="RGB", grain=True):
    """A black-and-white photograph: dark ground, a lighter rectangle where
    a figure's skin would be, with film grain so it has a photograph's
    spread of grey levels. `tint` makes it sepia; mode "L" saves it as a
    true greyscale file; grain=False draws it flat, like a diagram."""
    import io
    import random

    from PIL import Image, ImageDraw

    def shade(v):
        v = max(0, min(255, v))
        if tint is None:
            return (v, v, v)
        return (min(255, v + tint[0]), v, max(0, v - tint[1]))

    im = Image.new("RGB", size, shade(background))
    draw = ImageDraw.Draw(im)
    if light_box:
        draw.rectangle(light_box, fill=shade(light))
    if grain:
        rng = random.Random(7)
        px = im.load()
        for yy in range(size[1]):
            for xx in range(size[0]):
                r, g, b = px[xx, yy]
                d = rng.randint(-22, 22)
                px[xx, yy] = shade(r + d) if tint is None else (
                    max(0, min(255, r + d)), max(0, min(255, g + d)), max(0, min(255, b + d)))
    if mode == "L":
        im = im.convert("L")
    out = io.BytesIO(); im.save(out, "PNG")
    return out.getvalue()


def _code_screenshot(size=(300, 400)):
    """A terminal: light text lines on a near-black ground, two grey levels."""
    import io

    from PIL import Image, ImageDraw

    im = Image.new("RGB", size, (18, 18, 18))
    draw = ImageDraw.Draw(im)
    for row in range(10, size[1], 14):
        draw.rectangle((12, row, 12 + (row * 7) % 220 + 40, row + 6), fill=(200, 200, 200))
    out = io.BytesIO(); im.save(out, "PNG")
    return out.getvalue()


def test_black_and_white_and_sepia_are_monochrome_and_colour_is_not():
    assert vision.is_monochrome(_grey_photo((60, 60, 240, 400)))
    assert vision.is_monochrome(_grey_photo((60, 60, 240, 400), mode="L"))
    assert vision.is_monochrome(_grey_photo((60, 60, 240, 400), tint=(18, 14)))
    assert vision.is_monochrome(_code_screenshot())
    assert not vision.is_monochrome(_skin_photo(skin_box=(60, 60, 240, 400)))
    # A flat single-colour picture is not "monochrome": its chroma sits far
    # from neutral, so the colour gate still applies to it.
    assert not vision.is_monochrome(_skin_photo(skin_box=None))


def test_a_black_and_white_figure_with_bare_skin_is_promoted():
    # "black and white or monochromatic images aren't detected well": the
    # colour gate saw no skin at all in them. Face and body share a
    # lightness; the picture is measured by that.
    photo = _grey_photo(light_box=(60, 30, 240, 400))
    face = vision.Detection("FACE_FEMALE", 0.9, (120, 30, 60, 60))
    assert vision.skin_fraction(photo, vision.body_box(face.box, 300, 400)) > vision.SKIN_LIMIT
    verdict = vision.ImageFilter._person_aware(photo, [face], vision.judge([face]))
    assert verdict.level == vision.IMMODEST


def test_a_black_and_white_figure_in_dark_clothing_stays_clean():
    # The face is light; the body below it is dark cloth, nothing like the
    # face's lightness.
    photo = _grey_photo(light_box=(120, 30, 180, 90))  # the face only
    face = vision.Detection("FACE_FEMALE", 0.9, (120, 30, 60, 60))
    verdict = vision.ImageFilter._person_aware(photo, [face], vision.judge([face]))
    assert verdict.level == vision.CLEAN


def test_a_sepia_figure_is_measured_like_a_black_and_white_one():
    photo = _grey_photo(light_box=(60, 30, 240, 400), tint=(18, 14))
    face = vision.Detection("FACE_FEMALE", 0.9, (120, 30, 60, 60))
    assert vision.ImageFilter._person_aware(photo, [face], vision.judge([face])).level \
        == vision.IMMODEST
    clothed = _grey_photo(light_box=(120, 30, 180, 90), tint=(18, 14))
    assert vision.ImageFilter._person_aware(clothed, [face], vision.judge([face])).level \
        == vision.CLEAN


def test_the_person_detector_is_asked_about_a_black_and_white_picture(monkeypatch):
    from kosherd import persons

    # No face: the figure the detector finds is measured by the mid band
    # photographed skin falls in, so bare legs in a B&W shot are caught.
    photo = _grey_photo(light_box=(110, 200, 190, 400))
    monkeypatch.setattr(persons, "default", lambda: _Finds((90, 40, 120, 360)))
    verdict = vision.ImageFilter._person_aware(photo, [], vision.judge([]))
    assert verdict.level == vision.IMMODEST and verdict.has_person


def test_a_terminal_or_a_picture_of_code_is_never_skin(monkeypatch):
    # "the monochrome detection should not clobber tui images and code":
    # light text on a dark ground sits in the skin band, but it has a
    # drawing's few grey levels, not a photograph's spread. Even a stray
    # person detection over it measures nothing.
    from kosherd import persons

    shot = _code_screenshot()
    assert vision.skin_fraction(shot, (0, 0, 300, 400)) == 0.0
    assert vision.skin_fraction(shot, (0, 0, 300, 400), reference=200) == 0.0
    monkeypatch.setattr(persons, "default", lambda: _Finds((20, 20, 260, 360)))
    verdict = vision.ImageFilter._person_aware(shot, [], vision.judge([]))
    assert verdict.level == vision.CLEAN
    face = vision.Detection("FACE_FEMALE", 0.3, (120, 30, 60, 60))
    assert vision.ImageFilter._person_aware(shot, [face], vision.judge([face])).level \
        == vision.CLEAN
    # And a flat diagram in greys is a drawing too.
    flat = _grey_photo(light_box=(60, 30, 240, 400), grain=False)
    assert vision.skin_fraction(flat, (60, 30, 180, 370)) == 0.0


def test_a_figure_found_lying_flat_is_measured_over_its_middle_with_a_tighter_limit():
    # Gym photographs in sports bras and leggings, lying or lunging, came
    # back clean: the detector's box was mostly floor and the legs region
    # assumed somebody upright. The detector's box is tight around the
    # figure, so a lower fraction over it means the same thing, and its
    # middle is measured too.
    photo = _skin_photo(skin_box=(135, 104, 265, 158), size=(400, 300))
    box = (40, 100, 320, 120)  # wide: aspect 2.7; the skin sits above the legs region
    whole = vision.skin_fraction(photo, box)
    assert vision.FOUND_SKIN_LIMIT <= whole < vision.SKIN_LIMIT, whole
    assert not vision.shows_too_much(photo, box), "a face-estimated box keeps the old line"
    assert vision.shows_too_much(photo, box, found=True)
    faint = _skin_photo(skin_box=(170, 120, 230, 150), size=(400, 300))
    assert vision.skin_fraction(faint, box) < vision.FOUND_SKIN_LIMIT
    assert vision.skin_fraction(faint, vision.core_box(box)) < vision.SKIN_LIMIT
    assert not vision.shows_too_much(faint, box, found=True), "a little skin is still a little"


def test_the_legs_region_sits_in_the_lower_middle_of_the_figure():
    x, y, w, h = vision.legs_box((100, 0, 200, 800))
    assert (x, y, w, h) == (150, 400, 100, 280)


class _Finds:
    """A stand-in person detector: says there is a person here."""

    def __init__(self, box):
        self.box = box

    def detect(self, data, threshold=0.5):
        return [(0.9, self.box)] if self.box else []


def test_a_hips_down_skirt_shot_is_caught_when_the_person_detector_finds_the_figure(monkeypatch):
    # No face, no labelled part: the nudity model returns nothing and the
    # picture used to pass. The person detector says where the figure is,
    # and the legs region of that box is mostly skin.
    from kosherd import persons

    photo = _skin_photo(skin_box=(110, 200, 190, 400))       # legs, centred, lower half
    monkeypatch.setattr(persons, "default", lambda: _Finds((90, 40, 120, 360)))
    verdict = vision.ImageFilter._person_aware(photo, [], vision.judge([]))
    assert verdict.level == vision.IMMODEST
    assert verdict.regions == ((90, 40, 120, 360),)
    assert verdict.has_person


def test_a_clothed_figure_found_by_the_person_detector_counts_as_a_person_but_stays_clean(monkeypatch):
    from kosherd import persons

    # Enough skin in the frame (a bare arm at the edge) for the detector to
    # be asked at all; the figure it finds is clothed.
    photo = _skin_photo(skin_box=(0, 0, 120, 120))
    monkeypatch.setattr(persons, "default", lambda: _Finds((90, 40, 120, 360)))
    verdict = vision.ImageFilter._person_aware(photo, [], vision.judge([]))
    assert verdict.level == vision.CLEAN
    assert verdict.has_person, "a search thumbnail with this in it is still hidden"


def test_the_person_detector_is_not_asked_about_a_picture_with_no_skin_in_it(monkeypatch):
    from kosherd import persons

    asked = []

    class Counting(_Finds):
        def detect(self, data, threshold=0.5):
            asked.append(1)
            return super().detect(data, threshold)

    monkeypatch.setattr(persons, "default", lambda: Counting((0, 0, 100, 100)))
    verdict = vision.ImageFilter._person_aware(_skin_photo(None), [], vision.judge([]))
    assert verdict.level == vision.CLEAN and not asked


def test_no_person_model_means_the_old_answer_not_a_crash(monkeypatch):
    from kosherd import persons

    class Missing:
        def detect(self, data, threshold=0.5):
            return None

    monkeypatch.setattr(persons, "default", lambda: Missing())
    photo = _skin_photo(skin_box=(110, 200, 190, 400))
    verdict = vision.ImageFilter._person_aware(photo, [], vision.judge([]))
    assert verdict.level == vision.CLEAN and not verdict.has_person


def test_hidden_pictures_cover_the_whole_figure_not_a_fragment():
    # Legs under a short skirt: an exposed-class hit covered only its own
    # box; the rest of the person stayed visible.
    photo = _skin_photo(skin_box=(60, 60, 240, 400))
    face = vision.Detection("FACE_FEMALE", 0.9, (120, 30, 60, 60))
    belly = vision.Detection("BELLY_EXPOSED", 0.8, (130, 200, 40, 40))
    verdict = vision.ImageFilter._person_aware(photo, [face, belly],
                                               vision.judge([face, belly]))
    assert verdict.level != vision.CLEAN
    # a region at least as tall as the extrapolated body exists
    assert any(h > 300 for x, y, w, h in verdict.regions), verdict.regions


def test_a_judgement_change_invalidates_cached_verdicts(monkeypatch):
    # The cache is keyed by picture AND judgement version: after a fix to
    # the rules, a picture judged "clean" under the old rules must be
    # judged again, not served from cache.
    data = b"\x89PNG the same picture bytes"
    before = vision.digest(data)
    monkeypatch.setattr(vision, "JUDGEMENT_VERSION", vision.JUDGEMENT_VERSION + 1)
    assert vision.digest(data) != before


# -- statues and old paintings: a face of either sex, and a weak guess at a figure -----

def test_a_male_face_over_exposed_skin_is_promoted_too():
    # A nude male statue with a face the model called male came back clean
    # with over forty percent skin across the figure, because only a female
    # face used to anchor the measurement. A bare male chest is immodest by
    # label already; this measures it when the label does not fire.
    photo = _skin_photo(skin_box=(60, 60, 240, 400))
    face = vision.Detection("FACE_MALE", 0.9, (120, 30, 60, 60))
    verdict = vision.ImageFilter._person_aware(photo, [face], vision.judge([face]))
    assert verdict.level == vision.IMMODEST
    assert verdict.regions


def test_a_male_face_over_clothing_stays_clean():
    photo = _skin_photo(skin_box=None)
    face = vision.Detection("FACE_MALE", 0.9, (120, 30, 60, 60))
    verdict = vision.ImageFilter._person_aware(photo, [face], vision.judge([face]))
    assert verdict.level == vision.CLEAN


def test_a_figure_beside_its_face_is_measured_over_the_detectors_box(monkeypatch):
    # The Discobolus: bent double, the body is beside the face, not under
    # it, so the box guessed from the face (upright) is mostly background
    # and measures clean. The person detector's own box is what is bare.
    from kosherd import persons

    photo = _skin_photo(skin_box=(120, 20, 400, 200), size=(400, 300))   # a figure lying across the top
    face = vision.Detection("FACE_MALE", 0.9, (20, 40, 50, 50))          # face at the far left
    assert not vision.shows_too_much(photo, vision.body_box(face.box, 400, 300)), \
        "the upright guess misses it"
    monkeypatch.setattr(persons, "default", lambda: _Finds((20, 20, 380, 200)))
    verdict = vision.ImageFilter._person_aware(photo, [face], vision.judge([face]))
    assert verdict.level == vision.IMMODEST
    assert verdict.regions == ((20, 20, 380, 200),)


class _Guesses(_Finds):
    """A person detector that is not sure: returns its box at `score`,
    but only when asked down to that score."""

    def __init__(self, box, score):
        super().__init__(box)
        self.score = score

    def detect(self, data, threshold=0.5):
        return [(self.score, self.box)] if self.box and self.score >= threshold else []


def test_a_weak_guess_at_a_person_is_believed_over_a_bare_region(monkeypatch):
    # Classical statues: the person model hedges on marble (David 0.44,
    # the Farnese Hercules 0.22), below its own line — but the region it
    # names is mostly skin, and a sofa's never is.
    from kosherd import persons

    photo = _skin_photo(skin_box=(80, 40, 220, 380))
    monkeypatch.setattr(persons, "default", lambda: _Guesses((80, 40, 140, 340), 0.25))
    verdict = vision.ImageFilter._person_aware(photo, [], vision.judge([]))
    assert verdict.level == vision.IMMODEST
    assert verdict.has_person


def test_a_weak_guess_over_a_mostly_covered_region_is_not_believed(monkeypatch):
    # The same weak score over a figure that is only a little skin (an arm
    # at the edge): not enough to call it a person, so nothing is hidden.
    from kosherd import persons

    photo = _skin_photo(skin_box=(80, 40, 120, 380))                    # a strip: ~28% of the box
    monkeypatch.setattr(persons, "default", lambda: _Guesses((80, 40, 140, 340), 0.25))
    verdict = vision.ImageFilter._person_aware(photo, [], vision.judge([]))
    assert verdict.level == vision.CLEAN
    assert not verdict.has_person


def test_the_barer_the_region_the_weaker_the_guess_that_is_believed():
    from kosherd import persons

    assert vision.believable(persons.PERSON_CONFIDENCE, None), "a real detection needs no skin"
    assert not vision.believable(vision.WEAK_PERSON_CONFIDENCE - 0.01, 0.99), "below the floor, nothing helps"
    assert vision.believable(vision.WEAK_PERSON_CONFIDENCE, vision.BARE_SKIN_LIMIT)
    assert not vision.believable(vision.WEAK_PERSON_CONFIDENCE, vision.BARE_SKIN_LIMIT - 0.01)
    # Botticelli's Venus at thumbnail size: 0.41 with a third of the box skin.
    assert vision.believable(0.41, 0.35)
    assert not vision.believable(0.41, 0.20)


def test_a_face_is_left_out_of_a_found_figures_measurement(monkeypatch):
    # A head-and-shoulders portrait: the face is a third of the detector's
    # tight box, which on its own passes the line for a found figure. That
    # would hide every clothed portrait on a news page. The face is not
    # immodest and is not counted.
    from kosherd import persons

    photo = _skin_photo(skin_box=(100, 20, 200, 120), size=(300, 300))   # the face, and nothing else bare
    face = vision.Detection("FACE_FEMALE", 0.9, (100, 20, 100, 100))
    box = (80, 10, 140, 200)                                              # head and shoulders
    assert vision.skin_fraction(photo, box) > vision.FOUND_SKIN_LIMIT, "counted, the face alone trips it"
    assert vision.skin_fraction(photo, box, exclude=[face.box]) < 0.05
    monkeypatch.setattr(persons, "default", lambda: _Finds(box))
    verdict = vision.ImageFilter._person_aware(photo, [face], vision.judge([face]))
    assert verdict.level == vision.CLEAN
    assert verdict.has_person


def test_a_region_that_is_all_face_measures_as_nothing():
    photo = _skin_photo(skin_box=(0, 0, 300, 400))
    assert vision.skin_fraction(photo, (10, 10, 50, 50), exclude=[(0, 0, 300, 400)]) is None
