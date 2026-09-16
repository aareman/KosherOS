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

# A short skirt on its own. Measured over the whole figure, bare legs under
# a covered top come to a sixth of the box — under SKIN_LIMIT — and the
# picture sailed through; "miniskirts seem to get through a lot". So the
# legs are measured on their own: the middle half of the body box's width,
# from half way down to well above the feet (the floor is often beige),
# where a skirt that stops at the thigh leaves most of the region skin.
LEGS_SKIN_LIMIT = 0.30


def legs_box(body):
    """Where the legs are in an estimated body box: thighs to shins."""
    x, y, w, h = body
    return (int(x + 0.25 * w), int(y + 0.5 * h), int(0.5 * w), int(0.35 * h))


def _person_in(image_bytes: bytes, size) -> tuple | None:
    """The best person box the person detector finds, or None: no model,
    nobody, or too little skin-toned area for it to matter either way."""
    from . import persons as persons_mod

    whole = skin_fraction(image_bytes, (0, 0, *size))
    if whole is None or whole < persons_mod.WORTH_ASKING:
        return None
    found = persons_mod.default().detect(image_bytes)
    if not found:
        return None
    return found[0][1]


def shows_too_much(image_bytes: bytes, body) -> bool:
    """The figure, or its legs alone, past the immodest line."""
    fraction = skin_fraction(image_bytes, body)
    if fraction is not None and fraction >= SKIN_LIMIT:
        return True
    legs = skin_fraction(image_bytes, legs_box(body))
    return legs is not None and legs >= LEGS_SKIN_LIMIT

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


def skin_fraction(image_bytes: bytes, box) -> float | None:
    """How much of `box` is skin-toned, by the classic YCbCr gate."""
    try:
        import io

        from PIL import Image

        with Image.open(io.BytesIO(image_bytes)) as im:
            x, y, w, h = box
            region = im.convert("YCbCr").crop(
                (x, y, min(x + w, im.width), min(y + h, im.height)))
            if region.width < 8 or region.height < 8:
                return None
            # Sample down: precision is not needed to measure a fraction.
            region = region.resize((min(96, region.width),
                                    min(96, region.height)))
            data = region.getdata()
            skin = sum(1 for (Y, cb, cr) in data
                       if Y > 60 and 77 <= cb <= 127 and 133 <= cr <= 173)
            return skin / max(1, len(data))
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
    """Would an account at this media level hide this picture?"""
    threshold = HIDE_AT.get(media_level)
    if threshold is None:
        return False
    if threshold == CLEAN:
        return True  # "all": no judgement involved
    return verdict.at_least(threshold)


# Bump whenever the JUDGEMENT changes — label sets, thresholds, the skin
# rule, body extrapolation. The verdict cache is keyed by picture hash plus
# this, so a fix cannot be defeated by verdicts reached under the old
# logic: a family test found a swimsuit thumbnail still "clean" after the
# skin rule shipped, because its clean verdict from earlier was still in
# the cache.
JUDGEMENT_VERSION = 5  # 5: a person detector where the nudity model saw no face


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


# What OpenCV (NudeNet's reader) can open on its own; anything else is
# re-encoded to JPEG before detection.
CV2_FORMATS = frozenset({"JPEG", "PNG", "WEBP", "BMP", "TIFF"})


def prepare(image_bytes: bytes) -> tuple[bytes, float]:
    """What the detector is handed, and the factor its boxes must be
    divided by to land on the original picture.

    Small pictures blind the model: on ~200px search thumbnails the
    detector missed a swimsuit photo entirely — not even the face.
    Upscaling to ~640 on the short side before detection restores most of
    it, for a few milliseconds of Pillow. And NudeNet reads the file with
    OpenCV, which cannot open a GIF or an AVIF at all: every GIF was "could
    not judge" and hidden, static or not. Those become a JPEG of the
    (first) frame. Anything else goes through untouched.
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
# the rest, so it is sampled like one.
ANIMATION_FRAMES = 4


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

        known, sha = self._prejudge(image_bytes)
        if known is not None or sha is None:
            return known
        loop = asyncio.get_running_loop()
        future = loop.run_in_executor(self._executor(), self.judge_bytes,
                                      image_bytes, sha)
        try:
            return await asyncio.wait_for(asyncio.shield(future), self.timeout)
        except asyncio.TimeoutError:
            self._record(self.timeout * 1000)
            return None
        except Exception:  # noqa: BLE001 - a broken picture is not a verdict
            log.debug("judgement failed", exc_info=True)
            return None

    async def run_async(self, fn, *args):
        """Run CPU work (covering a picture, sampling a video) on the same
        small pool, off the event loop."""
        import asyncio

        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor(), fn, *args)

    @staticmethod
    def _person_aware(image_bytes, detections, verdict) -> ImageVerdict:
        """Whole figures, not fragments.

        Two family-test findings: a covered detection left the rest of the
        person visible (legs under a short skirt), and images with no
        detectable class at all — bare shoulders, exposed legs — came back
        clean. So: when a picture is being hidden and a face was found, the
        whole estimated figure joins the covered regions; and a clean
        picture with a female face is promoted to immodest when the figure
        shows too much skin for the labels to have caught.
        """
        try:
            import io

            from PIL import Image

            with Image.open(io.BytesIO(image_bytes)) as im:
                size = im.size
        except Exception:  # noqa: BLE001
            return verdict
        faces = [d.box for d in detections
                 if d.label in FACES and d.score >= MIN_CONFIDENCE]
        female = [d.box for d in detections
                  if d.label == "FACE_FEMALE" and d.score >= MIN_CONFIDENCE]
        if verdict.level != CLEAN and faces:
            bodies = tuple(body_box(f, *size) for f in faces)
            return ImageVerdict(verdict.level, verdict.regions + bodies,
                                verdict.has_person)
        if verdict.level == CLEAN and female:
            for face in female:
                if shows_too_much(image_bytes, body_box(face, *size)):
                    return ImageVerdict(
                        IMMODEST,
                        tuple(body_box(f, *size) for f in female),
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
        # Nothing to hang a body on — no face, and either no part at all or
        # nothing that said "too much". A skirt photographed from the hips
        # down is exactly this: legs are not a class. Ask the person
        # detector where the figure is, and measure the skin over that.
        if verdict.level == CLEAN and not faces:
            person = _person_in(image_bytes, size)
            if person is not None:
                if shows_too_much(image_bytes, person):
                    return ImageVerdict(IMMODEST, (person,), True)
                return ImageVerdict(CLEAN, (), True)
        return verdict
