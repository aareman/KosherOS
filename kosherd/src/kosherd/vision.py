"""Looking at pictures.

Word lists cannot read a photograph, and a filter that judges pages but
not their images is not a filter a family will trust. This is the part
that looks.

Three choices shape everything here.

**It runs on the device.** Sending every image a family looks at to a
service would be a worse privacy bargain than the one they are trying to
escape, and it would stop working the moment the connection did. So the
model is small, ONNX, CPU-only, and lives in the image.

**It detects rather than classifies.** A single "is this bad" score gives
you one number and no recourse; a detector gives labelled regions, which
is what makes the levels below meaningful and what lets a picture be
partly obscured instead of wholly removed.

**Regions get covered, not repainted.** Generative inpainting — actually
redrawing what was there — takes seconds per image on a GPU and is not
achievable on the low-end machine this has to work on. A heavy blur over
the detected region is instant, keeps the page's layout intact, and is
honest about what happened.

The detector is a plug-in point. Nothing here imports a model at module
scope, so the policy, the mapping and the caching are all testable, and a
machine where the model failed to install degrades to the level that
needs no judgement ("hide every picture") rather than to no filtering.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

CACHE_PATH = Path("/var/lib/kosher-mitm/images.sqlite")
# What the proxy tells the rest of the system about picture checking. The
# proxy is the only thing that knows whether the machine can keep up, and
# an admin who sees blank pictures deserves to be told why rather than
# left to guess that the filter is broken.
STATUS_PATH = Path("/var/lib/kosher-mitm/status.json")

CHECKING = "checking"
TOO_SLOW = "too_slow"
NO_MODEL = "no_model"

# Same ladder as the text scorer and the media levels a parent chooses.
CLEAN = "clean"
IMMODEST = "immodest"
SUGGESTIVE = "suggestive"
NSFW = "nsfw"
LEVELS = (CLEAN, IMMODEST, SUGGESTIVE, NSFW)
SEVERITY = {name: i for i, name in enumerate(LEVELS)}

# What a media_level setting actually hides. "none" hides nothing and
# "all" needs no detector at all, which is why it is the only setting that
# is right every time.
HIDE_AT = {
    "none": None,
    "nsfw": NSFW,
    "suggestive": SUGGESTIVE,
    "immodest": IMMODEST,
    "people": IMMODEST,   # and anyone at all: see hides()
    "all": CLEAN,
}

# NudeNet's labels, sorted into the ladder. Exposure of these is explicit
# under any reading.
NSFW_LABELS = frozenset({
    "FEMALE_GENITALIA_EXPOSED", "MALE_GENITALIA_EXPOSED", "ANUS_EXPOSED",
    "FEMALE_BREAST_EXPOSED", "BUTTOCKS_EXPOSED",
})
# Covered but prominent, or partial exposure: not explicit, plainly not
# something a filtered account asked to see either.
SUGGESTIVE_LABELS = frozenset({
    "FEMALE_BREAST_COVERED", "FEMALE_GENITALIA_COVERED", "BUTTOCKS_COVERED",
    "BELLY_EXPOSED", "ARMPITS_EXPOSED",
})
# Exposed skin beyond the face and hands. The detector has no label for
# bare arms or bare legs, so this level is genuinely weaker than the two
# above it — it catches what the model can see and no more, and the honest
# fallback for a family that wants tzniut enforced strictly is "all".
IMMODEST_LABELS = frozenset({
    "FEET_EXPOSED", "MALE_BREAST_EXPOSED", "BELLY_COVERED", "ARMPITS_COVERED",
})
FEMALE_LABELS = frozenset({"FACE_FEMALE", "FEMALE_BREAST_COVERED",
                           "FEMALE_BREAST_EXPOSED", "FEMALE_GENITALIA_COVERED",
                           "FEMALE_GENITALIA_EXPOSED"})

# Anything that says "there is a person in this picture". Reliable in a way
# the immodesty judgement is not: the detector has no label for a bare arm,
# but it is good at finding people.
PERSON_LABELS = (NSFW_LABELS | SUGGESTIVE_LABELS | IMMODEST_LABELS
                 # NudeNet spells faces FACE_FEMALE / FACE_MALE. These were
                 # FEMALE_FACE / MALE_FACE for weeks — labels the model never
                 # emits — so a detected face never counted as a person and
                 # every person-presence rule silently never fired.
                 | frozenset({"FACE_FEMALE", "FACE_MALE"}))

# Below this the detector is guessing. Deliberately low: a missed explicit
# region costs far more than a blurred elbow.
# 0.18 after immodest images slipped through on a celebrity site: the
# borderline detections are exactly the immodest ones, and for a modesty
# filter a false cover is a far smaller cost than a miss.
MIN_CONFIDENCE = 0.18
# A picture smaller than this is an icon, a spacer or a tracking pixel.
# 2500, not 6000: modern WebP/AVIF thumbnails fit a lot of person into
# very few bytes, and thumbnails were walking through the old gate.
MIN_IMAGE_BYTES = 2500
# Inference must not stall the page.
DETECT_TIMEOUT = 2.0

# Measured: the detector takes tens of milliseconds on a developer's
# machine and 200-600 ms per image on two cores, which is the machine this
# product is for. At that rate a news page with thirty photographs is
# fifteen seconds of waiting for a half-checked page — worse, for the
# person using it, than the setting that needs no model at all.
#
# So the machine is measured rather than assumed. Above this median, the
# filter stops trying to judge pictures and hides them instead: instant,
# never wrong, and honest about what the computer can do. It is a rolling
# window, so a machine that was briefly busy recovers on its own.
SLOW_DETECT_MS = 400
SLOW_WINDOW = 8
CACHE_TTL = 30 * 24 * 3600  # the same picture is the same picture


@dataclass(frozen=True)
class Detection:
    label: str
    score: float
    box: tuple[int, int, int, int]  # x, y, width, height


@dataclass(frozen=True)
class ImageVerdict:
    level: str
    regions: tuple[tuple[int, int, int, int], ...] = ()
    # Whether there is a person in the picture at all. Separate from the
    # level because the detector is reliable about this and unreliable
    # about immodesty, and the two are used differently.
    has_person: bool = False

    def at_least(self, level: str) -> bool:
        return SEVERITY[self.level] >= SEVERITY[level]


def level_of(detections) -> str:
    """The strongest thing the detector found."""
    labels = {d.label for d in detections if d.score >= MIN_CONFIDENCE}
    if labels & NSFW_LABELS:
        return NSFW
    if labels & SUGGESTIVE_LABELS:
        return SUGGESTIVE
    if labels & IMMODEST_LABELS:
        return IMMODEST
    return CLEAN


FACES = frozenset({"FACE_FEMALE", "FACE_MALE"})

# A face box says where a person is; the body hangs below it. There is no
# detector class for legs, knees or shoulders — a short skirt or a bare
# shoulder is invisible to the labels — so covering and skin measurement
# work on this estimated extent instead.
def body_box(face, width, height):
    x, y, w, h = face
    left = max(0, int(x - 1.1 * w))
    top = max(0, int(y - 0.6 * h))
    right = min(width, int(x + w + 1.1 * w))
    bottom = min(height, int(y + 8.5 * h))
    return (left, top, right - left, bottom - top)


# The fraction of skin-toned pixels in a region above which a person is
# showing too much for the immodest level: legs under a short skirt, bare
# shoulders and arms. Faces and hands alone in a body-sized region sit far
# below this; beachwear sits far above.
SKIN_LIMIT = 0.22
# The person detector's box is tight around the figure, where a box
# estimated from a face is a guess with room around it; less of it is
# background, so a lower fraction means the same thing. Measured on gym
# photographs in sports bras and leggings that came back clean: 0.17 over a
# figure lying flat, against 0.30 for the same clothing kneeling.
FOUND_SKIN_LIMIT = 0.17
# The centre of a figure's box, for a figure that is wide rather than tall
# (lying, lunging, bending): the legs region assumes somebody upright, and
# the whole box is mostly floor. Skin is measured over the middle too.
CORE = 0.7

# A short skirt on its own. Measured over the whole figure, bare legs under
# a covered top come to a sixth of the box — under SKIN_LIMIT — and the
# picture sailed through; "miniskirts seem to get through a lot". So the
# legs are measured on their own: the middle half of the body box's width,
# from half way down to well above the feet (the floor is often beige),
# where a skirt that stops at the thigh leaves most of the region skin.
LEGS_SKIN_LIMIT = 0.30

# A found figure's box is a rectangle; what is behind the figure is in it
# too, and warm wood, beige walls and book spines pass the skin-colour
# gate. A shiur's rabbi at his desk measured 0.186 over his box and was
# refused (#274). When a found figure's measurement crosses the line but
# the box is not plainly bare, the segmenter is asked which pixels are the
# person (persons.PersonMask) and the skin is measured over those alone:
# the same frame is 0.075. Over a figure that is bare, taking the
# background out can only raise the fraction, so nothing is let off by
# it. Above MASK_CEILING the box is bare whatever is behind the figure,
# and the model is not asked; under MASK_MIN_COVER the segmenter saw no
# figure where the detector did (marble, paint, a figure too small), and
# its mask is not believed.
MASK_CUT = 128          # of 255: how sure the segmenter must be of a pixel
MASK_CEILING = 0.40
MASK_MIN_COVER = 0.25


def legs_box(body):
    """Where the legs are in an estimated body box: thighs to shins."""
    x, y, w, h = body
    return (int(x + 0.25 * w), int(y + 0.5 * h), int(0.5 * w), int(0.35 * h))


# A picture with (almost) no colour — black and white, or sepia — defeats
# the YCbCr gate: every pixel has the same chroma, so either nothing reads
# as skin or everything does, and "black and white or monochromatic
# images aren't detected well". Chroma spread below MONO_CHROMA_SPREAD with
# the average chroma near neutral (under MONO_CHROMA_OFFSET; a sepia tint
# is a small constant offset, a blue sky is a large one) means colour
# cannot be used, and lightness is measured instead: in a monochrome
# photograph skin is the lightness of the face, so pixels within
# MONO_SKIN_TOLERANCE of the face's median lightness count as skin, and
# with no face to refer to the mid band photographed skin falls in.
MONO_CHROMA_SPREAD = 8.0
MONO_CHROMA_OFFSET = 30.0
MONO_SKIN_TOLERANCE = 28
MONO_SKIN_BAND = (95, 215)
# Lightness is only measured over a region that looks like a photograph.
# A terminal, a screenshot of code or a diagram is monochrome too, and its
# light text on a dark ground sits squarely in the skin band — "the
# monochrome detection should not clobber tui images and code". A
# photograph has hundreds of distinct grey levels; drawn text has a few
# dozen, and one of them owns most of the pixels.
PHOTO_MIN_LEVELS = 64
PHOTO_MAX_ONE_LEVEL = 0.5


def _chroma(im) -> tuple[float, float]:
    """(spread, offset) of the picture's chroma: how much Cb and Cr vary,
    and how far their average sits from neutral grey."""
    small = im.convert("RGB").convert("YCbCr")
    small.thumbnail((64, 64))
    data = list(small.getdata())
    n = max(1, len(data))
    mean_cb = sum(cb for _y, cb, _cr in data) / n
    mean_cr = sum(cr for _y, _cb, cr in data) / n
    spread = (sum(abs(cb - mean_cb) for _y, cb, _cr in data)
              + sum(abs(cr - mean_cr) for _y, _cb, cr in data)) / n
    return spread, abs(mean_cb - 128) + abs(mean_cr - 128)


def _is_monochrome(im) -> bool:
    spread, offset = _chroma(im)
    return spread < MONO_CHROMA_SPREAD and offset < MONO_CHROMA_OFFSET


def is_monochrome(image_bytes: bytes) -> bool:
    """Black and white, or a single tint such as sepia."""
    try:
        import io

        from PIL import Image

        with Image.open(io.BytesIO(image_bytes)) as im:
            return _is_monochrome(im)
    except Exception:  # noqa: BLE001
        return False


def looks_photographic(lightnesses) -> bool:
    """Whether a sampled region has a photograph's spread of grey levels
    rather than a drawing's few (text, a terminal, a chart)."""
    values = list(lightnesses)
    if not values:
        return False
    counts: dict[int, int] = {}
    for v in values:
        counts[v] = counts.get(v, 0) + 1
    return (len(counts) >= PHOTO_MIN_LEVELS
            and max(counts.values()) / len(values) <= PHOTO_MAX_ONE_LEVEL)


def face_lightness(image_bytes: bytes, face) -> int | None:
    """The median lightness of the middle of a face box: what skin looks
    like in this picture, for a monochrome measurement."""
    try:
        import io

        from PIL import Image

        with Image.open(io.BytesIO(image_bytes)) as im:
            x, y, w, h = face
            region = im.convert("RGB").convert("YCbCr").crop(
                (int(x + w * 0.25), int(y + h * 0.25),
                 int(x + w * 0.75), int(y + h * 0.75)))
            if region.width < 2 or region.height < 2:
                return None
            values = sorted(Y for Y, _cb, _cr in region.getdata())
            return values[len(values) // 2]
    except Exception:  # noqa: BLE001
        return None


# The person detector says "a person" at 0.5. Below that it is guessing —
# but a guess over a region that is mostly bare is not the same thing as
# a guess over a sofa. Classical statues are where this bit: the person
# model was trained on photographs of people, and on marble it hedges.
# Michelangelo's David in a wide shot of its hall scored 0.44, the Farnese
# Hercules 0.22, a close crop of a Rubens torso 0.23 — and each of those
# boxes was between half and nine tenths skin-toned. So a weak guess is
# believed when its region is bare enough, on a sliding scale: at
# WEAK_PERSON_CONFIDENCE the region must be BARE_SKIN_LIMIT skin, and the
# requirement falls to FOUND_SKIN_LIMIT (the ordinary line for a found
# figure) as the score approaches the detector's own line, so a near-miss
# at 0.41 with a third of its box skin — Botticelli's Venus at thumbnail
# size — is believed too. Measured on eighty-odd controls (animals, food,
# toys, furniture, clothed people, clothed statues), the only one the
# scale takes is a clothed marble statuette of Athena — marble reads as
# skin, and that is a price worth paying for the statues it was made for.
WEAK_PERSON_CONFIDENCE = 0.2
BARE_SKIN_LIMIT = 0.45


def believable(score: float, bare: float | None) -> bool:
    """Whether a person guess below the detector's line is taken, given
    how much of its region is skin."""
    from . import persons as persons_mod

    if score >= persons_mod.PERSON_CONFIDENCE:
        return True
    if score < WEAK_PERSON_CONFIDENCE or bare is None:
        return False
    span = persons_mod.PERSON_CONFIDENCE - WEAK_PERSON_CONFIDENCE
    how_sure = (score - WEAK_PERSON_CONFIDENCE) / span
    needed = BARE_SKIN_LIMIT - how_sure * (BARE_SKIN_LIMIT - FOUND_SKIN_LIMIT)
    return bare >= needed


def _person_in(image_bytes: bytes, size) -> tuple | None:
    """The best person box the person detector finds, or None: no model,
    nobody, or too little skin-toned area for it to matter either way.

    A confident detection is taken as it is; a weak one is taken only
    when the region it names is bare enough (see believable())."""
    from . import persons as persons_mod

    whole = skin_fraction(image_bytes, (0, 0, *size))
    if whole is None or whole < persons_mod.WORTH_ASKING:
        return None
    found = persons_mod.default().detect(image_bytes, threshold=WEAK_PERSON_CONFIDENCE)
    if not found:
        return None
    for score, box in found:  # best first
        if score >= persons_mod.PERSON_CONFIDENCE:
            return box
        if believable(score, skin_fraction(image_bytes, box)):
            return box
    return None


def core_box(body):
    """The middle of a figure's box, CORE of its width and height."""
    x, y, w, h = body
    margin = (1 - CORE) / 2
    return (int(x + margin * w), int(y + margin * h), int(CORE * w), int(CORE * h))


def shows_too_much(image_bytes: bytes, body, reference: int | None = None,
                   found: bool = False, faces=()) -> bool:
    """The figure, or its legs alone, past the immodest line. `reference`
    is the face's lightness, used when the picture has no colour; `found`
    says the box came from the person detector rather than a face, so it
    is tight around the figure and its middle is measured as well.

    `faces` are left out of the measurement. A face is not immodest, and
    in a head-and-shoulders portrait it is a third of the detector's tight
    box — enough on its own to pass the line for a found figure, which
    would hide every clothed portrait on a news page. In a box guessed
    from a face the face is a thirtieth of it and the exclusion is moot.

    A figure whose box measures past the line, but not as plainly bare,
    is measured again over its own pixels (see MASK_CEILING)."""
    limit = FOUND_SKIN_LIMIT if found else SKIN_LIMIT
    too_much, whole = _past_the_line(image_bytes, body, reference, found, faces, limit)
    if not too_much or whole is None or whole >= MASK_CEILING:
        return too_much
    mask = _figure_mask(image_bytes, body)
    if mask is None:
        return True
    return _past_the_line(image_bytes, body, reference, found, faces, limit, within=mask)[0]


def _past_the_line(image_bytes, body, reference, found, faces, limit, within=None):
    """(past the line?, the fraction over the whole box)."""
    fraction = skin_fraction(image_bytes, body, reference, exclude=faces, within=within)
    if fraction is not None and fraction >= limit:
        return True, fraction
    if found:
        core = skin_fraction(image_bytes, core_box(body), reference, exclude=faces,
                             within=within)
        if core is not None and core >= SKIN_LIMIT:
            return True, fraction
    legs = skin_fraction(image_bytes, legs_box(body), reference, exclude=faces, within=within)
    return (legs is not None and legs >= LEGS_SKIN_LIMIT), fraction


def _figure_mask(image_bytes: bytes, body):
    """The segmenter's person mask, when it can be had and it agrees with
    the detector that there is a figure in `body`; None otherwise."""
    from . import persons as persons_mod

    try:
        from PIL import Image

        mask = persons_mod.default_mask().mask(image_bytes)
        if mask is None:
            return None
        person = mask.person
        x, y, w, h = body
        region = person.crop((x, y, min(x + w, person.width), min(y + h, person.height)))
        if region.width < 8 or region.height < 8:
            return None
        sample = region.resize((min(96, region.width), min(96, region.height)),
                               Image.Resampling.NEAREST)
        data = list(sample.getdata())
        cover = sum(1 for p in data if p >= MASK_CUT) / max(1, len(data))
        return mask if cover >= MASK_MIN_COVER else None
    except Exception:  # noqa: BLE001 - a measurement, never a crash
        return None

# A body-part detection this weak is not a verdict on its own, but it is
# enough to say "there is a figure here" for the skin measurement.
PART_HINT_CONFIDENCE = 0.12


def _union_grown(boxes, width, height, grow: float = 1.0):
    """The box around all `boxes`, grown by `grow` of its size, clipped."""
    left = min(x for x, y, w, h in boxes)
    top = min(y for x, y, w, h in boxes)
    right = max(x + w for x, y, w, h in boxes)
    bottom = max(y + h for x, y, w, h in boxes)
    dw, dh = int((right - left) * grow), int((bottom - top) * grow)
    left, top = max(0, left - dw), max(0, top - dh)
    right, bottom = min(width, right + dw), min(height, bottom + dh)
    return (left, top, right - left, bottom - top)


def skin_fraction(image_bytes: bytes, box, reference: int | None = None,
                  exclude=(), within=None) -> float | None:
    """How much of `box` is skin-toned: by the classic YCbCr gate in a
    colour picture, by lightness in a monochrome photograph (see MONO_*),
    and nothing at all in a monochrome drawing — text, a terminal, code.
    Pixels inside any of the `exclude` boxes (faces) are not counted on
    either side of the fraction; a region that is all excluded is None.
    `within` is the segmenter's FigureMask (persons.py): only pixels it
    marks as a person, at MASK_CUT or above, are counted on either side,
    and in a monochrome picture — where the lightness band is all the
    colour gate has — what it marks as skin is what counts as skin."""
    try:
        import io

        from PIL import Image

        with Image.open(io.BytesIO(image_bytes)) as im:
            mono = _is_monochrome(im)
            x, y, w, h = box
            right, bottom = min(x + w, im.width), min(y + h, im.height)
            region = im.convert("RGB").convert("YCbCr").crop((x, y, right, bottom))
            if region.width < 8 or region.height < 8:
                return None
            # Sample down: precision is not needed to measure a fraction.
            # Nearest, not smoothed: smoothing invents grey levels between
            # a drawing's few, and a terminal would pass for a photograph.
            sample_w, sample_h = min(96, region.width), min(96, region.height)
            region = region.resize((sample_w, sample_h), Image.Resampling.NEAREST)
            data = list(region.getdata())
            keep = [True] * len(data)
            if exclude:
                # Which samples fall inside an excluded box, in the
                # sampled region's own coordinates.
                sx, sy = sample_w / (right - x), sample_h / (bottom - y)
                holes = [(int((ex - x) * sx), int((ey - y) * sy),
                          int((ex + ew - x) * sx), int((ey + eh - y) * sy))
                         for ex, ey, ew, eh in exclude]
                keep = [not any(hx0 <= i % sample_w < hx1 and hy0 <= i // sample_w < hy1
                                for hx0, hy0, hx1, hy1 in holes)
                        for i in range(len(data))]
            skin_marks = None
            if within is not None:
                def sampled(mask):
                    return list(mask.crop((x, y, right, bottom)).resize(
                        (sample_w, sample_h), Image.Resampling.NEAREST).getdata())
                person = sampled(within.person)
                keep = [k and p >= MASK_CUT for k, p in zip(keep, person)]
                if mono and within.skin is not None:
                    skin_marks = sampled(within.skin)
            counted = [px for px, k in zip(data, keep) if k]
            if not counted:
                return None
            if skin_marks is not None:
                skin = sum(1 for mark, k in zip(skin_marks, keep) if k and mark >= MASK_CUT)
            elif mono:
                if not looks_photographic(Y for Y, _cb, _cr in counted):
                    return 0.0
                if reference is not None:
                    low, high = reference - MONO_SKIN_TOLERANCE, reference + MONO_SKIN_TOLERANCE
                else:
                    low, high = MONO_SKIN_BAND
                skin = sum(1 for (Y, _cb, _cr) in counted if Y > 60 and low <= Y <= high)
            else:
                skin = sum(1 for (Y, cb, cr) in counted
                           if Y > 60 and 77 <= cb <= 127 and 133 <= cr <= 173)
            return skin / max(1, len(counted))
    except Exception:  # noqa: BLE001 - a measurement, never a crash
        return None


def judge(detections) -> ImageVerdict:
    """A verdict, with the regions worth covering."""
    kept = [d for d in detections if d.score >= MIN_CONFIDENCE]
    level = level_of(kept)
    people = tuple(d.box for d in kept if d.label in PERSON_LABELS)
    if level == CLEAN:
        return ImageVerdict(CLEAN, (), bool(people))
    covered = NSFW_LABELS | SUGGESTIVE_LABELS | IMMODEST_LABELS
    regions = tuple(d.box for d in kept if d.label in covered)
    return ImageVerdict(level, regions, bool(people))


def in_context(verdict: "ImageVerdict", page_level: str, tolerance: str) -> bool:
    """Should this picture be hidden because of the page it is on?

    The immodest level is the weak one: the detector has no label for a
    bare arm or a bare leg, so a clothed model in a lingerie catalogue
    comes back clean and the picture stays on screen next to the word
    "lingerie". Two signals we already have fix most of that without a
    model that does not exist — the page's own words, and whether there is
    a person in the picture at all.

    Neither alone would do. Hiding every picture on a page that scored
    immodest would take out the shop's logo and its navigation icons;
    hiding every picture containing a person would take out a news
    photograph on a page about nothing in particular.
    """
    if not verdict.has_person or not page_level:
        return False
    return SEVERITY.get(page_level, 0) >= SEVERITY.get(tolerance, 99)


def hides(media_level: str, verdict: ImageVerdict) -> bool:
    """Would an account at this media level hide this picture?

    "people" hides everything "immodest" does and, besides, any picture
    with a person in it, whatever they wear. Tight or sheer clothing on a
    covered figure — "sensual tight and/or transparent clothing gets
    through like comic book super women" — is not something a detector
    can judge from skin or from labels, and a level that pretended to
    would be wrong quietly. This one is not: a person is what the models
    are reliable about, and landscapes, products, diagrams and text keep
    showing.
    """
    threshold = HIDE_AT.get(media_level)
    if threshold is None:
        return False
    if threshold == CLEAN:
        return True  # "all": no judgement involved
    if media_level == "people" and verdict.has_person:
        return True
    return verdict.at_least(threshold)


# Bump whenever the JUDGEMENT changes — label sets, thresholds, the skin
# rule, body extrapolation. The verdict cache is keyed by picture hash plus
# this, so a fix cannot be defeated by verdicts reached under the old
# logic: a family test found a swimsuit thumbnail still "clean" after the
# skin rule shipped, because its clean verdict from earlier was still in
# the cache.
JUDGEMENT_VERSION = 9  # 9: AVIF receives full refinement; unreadable images are not clean


def digest(data: bytes) -> str:
    return hashlib.sha256(data + f":judgement-v{JUDGEMENT_VERSION}".encode()).hexdigest()


_SCHEMA = """
CREATE TABLE IF NOT EXISTS images (
    sha     TEXT PRIMARY KEY,
    level   TEXT NOT NULL,
    regions TEXT NOT NULL,
    seen    INTEGER NOT NULL,
    person  INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS videos (
    key     TEXT PRIMARY KEY,
    level   TEXT NOT NULL,
    seen    INTEGER NOT NULL,
    person  INTEGER NOT NULL DEFAULT 0
);
"""


class VerdictCache:
    """Verdicts by image content hash.

    The same logo, banner and avatar come back on every page of a site, and
    inference is thousands of times more expensive than a hash lookup.
    """

    def __init__(self, path: Path = CACHE_PATH, ttl: int = CACHE_TTL):
        self.path = Path(path)
        self.ttl = ttl
        self._lock = threading.Lock()
        self._db = None

    def _conn(self):
        if self._db is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._db = sqlite3.connect(self.path, check_same_thread=False)
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.executescript(_SCHEMA)
        return self._db

    def get(self, sha: str) -> ImageVerdict | None:
        try:
            with self._lock:
                row = self._conn().execute(
                    "SELECT level, regions, seen, person FROM images "
                    "WHERE sha = ?", (sha,)).fetchone()
        except sqlite3.Error:
            return None
        if not row or time.time() - row[2] > self.ttl:
            return None
        return ImageVerdict(row[0], _unpack(row[1]), bool(row[3]))

    def put(self, sha: str, verdict: ImageVerdict) -> None:
        try:
            with self._lock:
                db = self._conn()
                db.execute(
                    "INSERT INTO images (sha, level, regions, seen, person) "
                    "VALUES (?, ?, ?, ?, ?) ON CONFLICT(sha) DO UPDATE SET "
                    "level=excluded.level, regions=excluded.regions, "
                    "seen=excluded.seen, person=excluded.person",
                    (sha, verdict.level, _pack(verdict.regions),
                     int(time.time()), int(verdict.has_person)))
                db.commit()
        except sqlite3.Error:
            log.debug("could not cache an image verdict", exc_info=True)

    # Clips, by address (see videocheck.key): same shape, no regions.
    def get_video(self, key: str) -> ImageVerdict | None:
        try:
            with self._lock:
                row = self._conn().execute(
                    "SELECT level, seen, person FROM videos WHERE key = ?",
                    (key,)).fetchone()
        except sqlite3.Error:
            return None
        if not row or time.time() - row[1] > self.ttl:
            return None
        return ImageVerdict(row[0], (), bool(row[2]))

    def put_video(self, key: str, verdict: ImageVerdict) -> None:
        try:
            with self._lock:
                db = self._conn()
                db.execute(
                    "INSERT INTO videos (key, level, seen, person) "
                    "VALUES (?, ?, ?, ?) ON CONFLICT(key) DO UPDATE SET "
                    "level=excluded.level, seen=excluded.seen, "
                    "person=excluded.person",
                    (key, verdict.level, int(time.time()), int(verdict.has_person)))
                db.commit()
        except sqlite3.Error:
            log.debug("could not cache a video verdict", exc_info=True)


def _pack(regions) -> str:
    return ";".join(",".join(str(int(v)) for v in box) for box in regions)


def _unpack(text: str) -> tuple:
    if not text:
        return ()
    boxes = []
    for chunk in text.split(";"):
        parts = chunk.split(",")
        if len(parts) == 4:
            try:
                boxes.append(tuple(int(p) for p in parts))
            except ValueError:
                continue
    return tuple(boxes)


# Conservative formats passed directly to OpenCV (NudeNet's reader).
# Codec support varies by build; other Pillow-readable formats are
# normalized to JPEG before detection even if this OpenCV can read them.
CV2_FORMATS = frozenset({"JPEG", "PNG", "WEBP", "BMP", "TIFF"})


def prepare(image_bytes: bytes) -> tuple[bytes, float]:
    """What the detector is handed, and the factor its boxes must be
    divided by to land on the original picture.

    Small pictures blind the model: on ~200px search thumbnails the
    detector missed a swimsuit photo entirely — not even the face.
    Upscaling to ~640 on the short side before detection restores most of
    it, for a few milliseconds of Pillow. NudeNet reads the file with
    OpenCV, whose codecs vary by build. Formats outside the conservative
    pass-through set, including GIF and AVIF, become a JPEG of the first
    frame when Pillow can read them. Other pictures go through untouched
    when no upscaling is needed.
    """
    scale = 1.0
    try:
        import io

        from PIL import Image

        with Image.open(io.BytesIO(image_bytes)) as im:
            short = min(im.size)
            foreign = (im.format or "").upper() not in CV2_FORMATS
            if 0 < short < 480:
                scale = min(640 / short, 4.0)
            if scale == 1.0 and not foreign:
                return image_bytes, 1.0
            frame = im.convert("RGB")
            if scale != 1.0:
                frame = frame.resize((int(im.width * scale), int(im.height * scale)),
                                     Image.Resampling.LANCZOS)
            out = io.BytesIO()
            frame.save(out, "JPEG", quality=90)
            return out.getvalue(), scale
    except Exception:  # noqa: BLE001 - detection on the original instead
        return image_bytes, 1.0
# Frames judged from an animated picture, spread through it. A GIF or an
# animated PNG/WebP is a short clip: a clean first frame says nothing about
# the rest, so it is sampled like one — and given as many deadlines as it
# has frames to judge, because four detections inside one picture's
# deadline timed out on a two-core machine and hid every GIF.
ANIMATION_FRAMES = 4


def looks_animated(image_bytes: bytes) -> bool:
    """Whether a picture has more than one frame, read from its header."""
    try:
        import io

        from PIL import Image

        with Image.open(io.BytesIO(image_bytes)) as im:
            return bool(getattr(im, "is_animated", False))
    except Exception:  # noqa: BLE001
        return False


def animation_frames(image_bytes: bytes, max_frames: int = ANIMATION_FRAMES):
    """Up to `max_frames` frames of an animated picture as JPEGs, spread
    through the animation; None for a still picture (or one PIL cannot
    open, which the caller treats as a still and lets the detector refuse)."""
    try:
        import io

        from PIL import Image

        with Image.open(io.BytesIO(image_bytes)) as im:
            count = getattr(im, "n_frames", 1)
            if not getattr(im, "is_animated", False) or count < 2:
                return None
            picks = sorted({int(count * (i + 0.5) / max_frames) for i in range(max_frames)})
            frames = []
            for index in picks:
                im.seek(min(index, count - 1))
                out = io.BytesIO()
                im.convert("RGB").save(out, "JPEG", quality=85)
                frames.append(out.getvalue())
            return frames
    except Exception:  # noqa: BLE001 - not an animation we can read
        return None


def combine(verdicts) -> ImageVerdict | None:
    """One verdict for a set of frames: the strongest, and whether anyone
    was in any of them. Regions are dropped — a cover placed on one frame
    of an animation means nothing on the others, so an animation that
    hides is hidden whole. None if nothing could be judged."""
    judged = [v for v in verdicts if v is not None]
    if not judged:
        return None
    worst = max(judged, key=lambda v: SEVERITY[v.level])
    return ImageVerdict(worst.level, (), any(v.has_person for v in judged))


class NullDetector:
    """What runs when no model is installed.

    It reports nothing rather than reporting "clean", so the caller can
    tell the difference between "looked and found nothing" and "could not
    look" — and fall back to hiding rather than to showing.
    """

    available = False

    def detect(self, image_bytes: bytes):
        return None


class NudeNetDetector:
    """NudeNet's ONNX detector: about 5 MB, CPU, tens of milliseconds.

    Imported lazily and behind a try: a machine where the model failed to
    install must degrade to hiding pictures, not to showing everything.
    """

    def __init__(self):
        self._detector = None
        self._failed = False
        self._lock = threading.Lock()

    @property
    def available(self) -> bool:
        return self._load() is not None

    def _load(self):
        if self._detector is not None or self._failed:
            return self._detector
        with self._lock:
            if self._detector is None and not self._failed:
                try:
                    from nudenet import NudeDetector

                    self._detector = NudeDetector()
                except Exception:  # noqa: BLE001 - absence is a supported state
                    log.warning("no image model installed; accounts with "
                                "picture filtering will hide pictures instead",
                                exc_info=True)
                    self._failed = True
        return self._detector

    def detect(self, image_bytes: bytes):
        detector = self._load()
        if detector is None:
            return None
        import tempfile

        # Small pictures blind the model: on ~200px search thumbnails the
        # detector missed a swimsuit photo entirely — not even the face.
        # Upscaling to ~640 on the short side before detection restores
        # most of it, for a few milliseconds of Pillow. The BOXES scale
        # back down so covers land on the original image.
        image_bytes, scale = prepare(image_bytes)

        # NudeNet reads a path, not bytes.
        with tempfile.NamedTemporaryFile(suffix=".img") as handle:
            handle.write(image_bytes)
            handle.flush()
            try:
                raw = detector.detect(handle.name)
            except Exception:  # noqa: BLE001 - a broken image is not a verdict
                log.debug("detection failed", exc_info=True)
                return None
        found = []
        for item in raw or []:
            box = item.get("box") or [0, 0, 0, 0]
            try:
                found.append(Detection(
                    label=str(item.get("class", "")),
                    score=float(item.get("score", 0.0)),
                    # Detection ran on the upscaled copy; the boxes must
                    # land on the ORIGINAL image the caller will edit.
                    box=(int(box[0] / scale), int(box[1] / scale),
                         int(box[2] / scale), int(box[3] / scale))))
            except (TypeError, ValueError, IndexError):
                continue
        return found


# The scheduling priority (nice) detection threads run at. Linux applies
# nice per thread, so the proxy's network handling keeps its priority and
# only the model's arithmetic yields to the browser.
WORKER_NICE = 15


def default_workers() -> int:
    """How many pictures to judge at once: one on a two-core machine, so a
    core is always left for the browser; two on anything larger. A third
    never helped — the detector already uses more than one thread."""
    import os

    cpus = os.cpu_count() or 2
    return 1 if cpus <= 2 else 2


def _lower_priority() -> None:
    import os

    try:
        os.setpriority(os.PRIO_PROCESS, 0, WORKER_NICE)  # 0: this thread
    except (AttributeError, OSError):
        pass


class ImageFilter:
    """Detector, cache and a deadline, wired together.

    Inference happens off the caller's thread with a hard timeout: a slow
    picture must not hold up a page. A picture that cannot be judged in
    time is treated as unjudged, and the caller hides it — the safe
    direction, and the one a person can understand ("pictures are off
    while this is busy" beats "sometimes it lets things through").
    """

    def __init__(self, detector=None, cache: VerdictCache | None = None,
                 timeout: float = DETECT_TIMEOUT, workers: int | None = None,
                 slow_ms: float = SLOW_DETECT_MS):
        self.detector = detector if detector is not None else NudeNetDetector()
        self.cache = cache if cache is not None else VerdictCache()
        self.timeout = timeout
        self._pool = None
        self._workers = workers if workers is not None else default_workers()
        self._slow_ms = slow_ms
        self._recent: list[float] = []
        self._said_slow = False
        self._published = None
        self._async_slots = None
        self._prepare_slots = None
        self._prepare_pool = None
        self._inflight = {}
        self._closed = False

    def status(self) -> dict:
        """What to tell an admin about picture checking on this machine."""
        if not self.available:
            state = NO_MODEL
        elif self.degraded:
            state = TOO_SLOW
        else:
            state = CHECKING
        median = None
        if self._recent:
            ordered = sorted(self._recent)
            median = round(ordered[len(ordered) // 2])
        return {"pictures": state, "detect_ms": median,
                "samples": len(self._recent)}

    def write_status(self, path: Path = None) -> None:
        """Publish the status, but only when it has changed.

        Written by the proxy, read by kosherd. A file rather than a bus
        call because the proxy is unprivileged and already talks to the
        rest of the system this way.
        """
        status = self.status()
        if status["pictures"] == self._published:
            return
        self._published = status["pictures"]
        target = Path(path) if path is not None else STATUS_PATH
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_suffix(".tmp")
            tmp.write_text(json.dumps(status) + "\n")
            tmp.replace(target)
        except OSError:
            log.debug("could not publish picture status", exc_info=True)

    @property
    def degraded(self) -> bool:
        """True when this machine cannot judge pictures fast enough.

        The caller hides pictures instead of showing unchecked ones. Worth
        surfacing to an admin: "this computer is too slow to check
        pictures, so it hides them" is a thing a person can act on, and
        "the web is slow today" is not.
        """
        if len(self._recent) < SLOW_WINDOW:
            return False
        ordered = sorted(self._recent)
        median = ordered[len(ordered) // 2]
        return median > self._slow_ms

    def _record(self, elapsed_ms: float) -> None:
        self._recent.append(elapsed_ms)
        del self._recent[:-SLOW_WINDOW]
        if self.degraded and not self._said_slow:
            self._said_slow = True
            log.warning(
                "picture checks take %.0f ms on this machine; hiding "
                "pictures instead of checking them", elapsed_ms)
        elif not self.degraded:
            self._said_slow = False

    @property
    def available(self) -> bool:
        return bool(getattr(self.detector, "available", False))

    def _executor(self):
        if self._pool is None:
            from concurrent.futures import ThreadPoolExecutor

            # Small on purpose: this runs beside the browser on a machine
            # that may have two cores — and at low priority, so when the
            # two compete for a core the browser wins. The person feels a
            # slow picture as a picture that arrives late; they feel a slow
            # browser as a slow computer.
            self._pool = ThreadPoolExecutor(max_workers=self._workers,
                                            thread_name_prefix="kosher-vision",
                                            initializer=_lower_priority)
        return self._pool

    def _prejudge(self, image_bytes: bytes):
        """The free answers: too small, already known, machine too slow.

        Returns (verdict_or_None, sha). A verdict means the answer is final
        without the model; a None with a sha means the model must look.
        None with no sha means the machine is degraded: could not judge.
        """
        if len(image_bytes) < MIN_IMAGE_BYTES:
            # Icons, spacers and tracking pixels: not worth the model's
            # time and not worth hiding either.
            return ImageVerdict(CLEAN, ()), None
        sha = digest(image_bytes)
        cached = self.cache.get(sha)
        if cached is not None:
            # Always served: a verdict already reached costs nothing and
            # is just as accurate on a slow machine as on a fast one.
            return cached, None
        if self.degraded:
            return None, None
        return None, sha

    def judge_bytes(self, image_bytes: bytes, sha: str | None = None) -> ImageVerdict | None:
        """The whole judgement, on the calling thread: detect, map, refine,
        cache. This is what the worker pool runs; a caller that gave up
        waiting still gets the verdict cached for the next time the same
        picture comes past, which on a slow machine is exactly when it
        matters. An animated picture is judged on frames spread through it
        and gets one verdict for the lot."""
        frames = animation_frames(image_bytes)
        if frames:
            verdict = combine(self._judge_one(frame) for frame in frames)
        else:
            verdict = self._judge_one(image_bytes)
        if verdict is None:
            return None
        self.cache.put(sha or digest(image_bytes), verdict)
        return verdict

    def _judge_one(self, image_bytes: bytes) -> ImageVerdict | None:
        started = time.monotonic()
        try:
            detections = self.detector.detect(image_bytes)
        except Exception:  # noqa: BLE001 - a broken image is not a verdict
            log.debug("detection failed", exc_info=True)
            self._record((time.monotonic() - started) * 1000)
            return None
        self._record((time.monotonic() - started) * 1000)
        if detections is None:
            return None
        verdict = judge(detections)
        return self._person_aware(image_bytes, detections, verdict)

    def verdict(self, image_bytes: bytes) -> ImageVerdict | None:
        """Judge one picture, waiting up to the deadline. None means it
        could not be judged (or not in time)."""
        known, sha = self._prejudge(image_bytes)
        if known is not None or sha is None:
            return known
        future = self._executor().submit(self.judge_bytes, image_bytes, sha)
        try:
            return future.result(timeout=self.timeout)
        except Exception:  # noqa: BLE001 - includes the timeout
            # The worker keeps going and caches its answer; the timeout is
            # counted against the machine so a slow one degrades honestly.
            self._record(self.timeout * 1000)
            return None

    async def verdict_async(self, image_bytes: bytes) -> ImageVerdict | None:
        """The same judgement, without holding the caller's event loop.

        The proxy runs its hooks on one asyncio loop. The synchronous
        `verdict` waited on the worker from that loop, so every other
        connection on the machine stood still for up to the deadline while
        one picture was judged — a page with thirty photographs froze the
        browser for the lot of them. Awaiting the worker instead lets the
        other flows carry on; only the picture being judged waits.
        """
        import asyncio

        if len(image_bytes) < MIN_IMAGE_BYTES:
            return ImageVerdict(CLEAN, ())
        started = time.monotonic()
        try:
            known, sha, animated = await asyncio.wait_for(
                self.prepare_async(self._prepare_image, image_bytes),
                self.timeout)
            if known is not None or sha is None:
                return known
            deadline = self.timeout * (ANIMATION_FRAMES if animated else 1)
            remaining = deadline - (time.monotonic() - started)
            if remaining <= 0:
                return None
            return await asyncio.wait_for(
                self.run_async(self._judge_if_needed, image_bytes, sha,
                               share_key=("image", sha)), remaining)
        except asyncio.TimeoutError:
            # Queue time is not detector time. A waiter that expires must
            # never become CPU work later, or make the machine look slow
            # simply because a page requested many pictures at once.
            return None
        except Exception:  # noqa: BLE001 - a broken picture is not a verdict
            log.debug("judgement failed", exc_info=True)
            return None

    def _prepare_image(self, image_bytes):
        # Hashing, SQLite locks/I/O and image headers all belong off the
        # network loop. A separate single worker keeps cached images from
        # waiting behind inference; admission bounds its submissions too.
        known, sha = self._prejudge(image_bytes)
        animated = looks_animated(image_bytes) if sha is not None else False
        return known, sha, animated

    def _judge_if_needed(self, image_bytes, sha):
        # A copy of this picture may have finished while this one waited
        # for a worker. Do not run the same inference again.
        cached = self.cache.get(sha)
        if cached is not None:
            return cached
        if self.degraded:
            return None
        return self.judge_bytes(image_bytes, sha)

    async def prepare_async(self, fn, *args):
        """Run cache/header/response preparation off the network loop.

        Admission is bounded separately from detection, so a busy model
        cannot prevent a cache hit or a safe fallback. Callers apply their
        request deadline; expired waiters never submit work.
        """
        return await self._run_async(fn, *args, preparation=True)

    async def run_async(self, fn, *args, share_key=None):
        """Admit CPU work only when a worker is free.

        Waiting stays in asyncio, where a cancelled/timed-out request can
        leave without ever submitting a job. A running job owns its slot
        until the actual thread finishes, even when its caller leaves.
        This bounds executor submissions across images, covers and clips.
        A share_key joins identical running work without occupying another
        worker. Namespace keys by operation; the function and arguments
        must produce the same result for every caller of that key.
        """
        return await self._run_async(fn, *args, share_key=share_key)

    async def _run_async(self, fn, *args, preparation=False, share_key=None):
        import asyncio
        from concurrent.futures import ThreadPoolExecutor

        async def wait_result(result):
            # asyncio.wait does not cancel the shared worker when its waiter
            # leaves. Unlike shield on Python 3.14, it also does not report
            # an abandoned failure separately from our completion observer.
            await asyncio.wait((result,))
            return result.result()

        if self._closed:
            raise RuntimeError("image filter is closed")
        loop = asyncio.get_running_loop()
        if share_key is not None and share_key in self._inflight:
            return await wait_result(self._inflight[share_key])
        if preparation:
            if self._prepare_slots is None:
                self._prepare_slots = asyncio.Semaphore(1)
                self._prepare_pool = ThreadPoolExecutor(
                    max_workers=1, thread_name_prefix="kosher-image-cache",
                    initializer=_lower_priority)
            slots = self._prepare_slots
            executor = self._prepare_pool
        else:
            if self._async_slots is None:
                self._async_slots = asyncio.Semaphore(self._workers)
            slots = self._async_slots
            executor = self._executor()
        await slots.acquire()
        # Another copy may have obtained a worker while we waited. Join
        # its result in asyncio, never by parking a detector thread.
        if share_key is not None and share_key in self._inflight:
            slots.release()
            return await wait_result(self._inflight[share_key])
        try:
            future = executor.submit(fn, *args)
        except BaseException:
            slots.release()
            raise

        result = asyncio.wrap_future(future, loop=loop)
        if share_key is not None:
            self._inflight[share_key] = result

        def finished(done):
            # Retire the job before waking its waiters, so an immediate
            # retry cannot rejoin a completed failure. This callback runs
            # only after the actual worker finishes, even if callers cancel.
            slots.release()
            if share_key is not None:
                self._inflight.pop(share_key, None)
            if not done.cancelled():
                done.exception()  # observe failures after every caller left

        result.add_done_callback(finished)
        return await wait_result(result)

    def close(self, *, wait=False):
        """Stop accepting work; let already-running checks finish safely.

        The proxy must not block its event loop during shutdown. Tests and
        command-line tools can request wait=True from outside that loop.
        """
        self._closed = True
        for pool in (self._prepare_pool, self._pool):
            if pool is not None:
                pool.shutdown(wait=wait, cancel_futures=True)

    @staticmethod
    def _person_aware(image_bytes, detections, verdict) -> ImageVerdict | None:
        """Whole figures, not fragments.

        Two family-test findings: a covered detection left the rest of the
        person visible (legs under a short skirt), and images with no
        detectable class at all — bare shoulders, exposed legs — came back
        clean. So: when a picture is being hidden and a face was found, the
        whole estimated figure joins the covered regions; and a clean
        picture with a face is promoted to immodest when the figure shows
        too much skin for the labels to have caught. Classical statues and
        old paintings of nudes are what the later steps are for: the
        models hedge on marble and on paint, so a face of either sex, the
        person detector's own box, and even a weak person guess over a
        mostly bare region each get a turn before a picture is called
        clean.
        """
        try:
            import io

            from PIL import Image

            with Image.open(io.BytesIO(image_bytes)) as im:
                size = im.size
        except Exception:  # noqa: BLE001
            # The base detector can decode formats this Pillow build
            # cannot. That is only a partial judgement: missing AVIF used
            # to skip every skin/person refinement and cache CLEAN.
            log.debug("image cannot be opened for refinement", exc_info=True)
            return None
        faces = [d.box for d in detections
                 if d.label in FACES and d.score >= MIN_CONFIDENCE]
        if verdict.level != CLEAN and faces:
            bodies = tuple(body_box(f, *size) for f in faces)
            return ImageVerdict(verdict.level, verdict.regions + bodies,
                                verdict.has_person)
        # Any face, not only a female one. The rule began as female-only
        # so that a man in short sleeves would not trip it, but it left
        # every figure the model called male unmeasured — and the model's
        # sexing of a face is a coin toss on a statue. A nude male statue
        # with a face the model called male came back clean with over
        # forty percent skin across the figure, while the same statue at
        # thumbnail size, where the model found no face, was caught by the
        # person-detector path below. A bare male chest is already
        # immodest by label (MALE_BREAST_EXPOSED); this measures it when
        # the label does not fire.
        if verdict.level == CLEAN and faces:
            for face in faces:
                reference = face_lightness(image_bytes, face)
                if shows_too_much(image_bytes, body_box(face, *size), reference):
                    return ImageVerdict(
                        IMMODEST,
                        tuple(body_box(f, *size) for f in faces),
                        True)
        # No face in frame — a figure seen from behind, say — but body parts
        # were found. A woman in a sports shirt photographed from the back
        # came back clean: no face to hang a body box on, no exposed class.
        # Measure skin over the whole detected figure instead.
        parts = [d.box for d in detections
                 if d.label in PERSON_LABELS and d.label not in FACES
                 and d.score >= PART_HINT_CONFIDENCE]
        if verdict.level == CLEAN and parts and not faces:
            figure = _union_grown(parts, *size)
            fraction = skin_fraction(image_bytes, figure)
            if fraction is not None and fraction >= SKIN_LIMIT:
                return ImageVerdict(IMMODEST, (figure,), True)
        # Still clean. Either nothing anchored a body — no face, and no part
        # that said "too much"; a skirt photographed from the hips down is
        # exactly this, legs are not a class — or a face did, and the box
        # guessed from it (upright, face on top) measured clean. That guess
        # misses a figure bent, crouching or thrown: the Discobolus's body
        # is beside its face, not under it. Ask the person detector where
        # the figure actually is, and measure the skin over that.
        if verdict.level == CLEAN:
            person = _person_in(image_bytes, size)
            if person is not None:
                if shows_too_much(image_bytes, person, found=True, faces=faces):
                    return ImageVerdict(IMMODEST, (person,), True)
                return ImageVerdict(CLEAN, (), True)
        return verdict
