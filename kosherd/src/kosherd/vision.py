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


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


_SCHEMA = """
CREATE TABLE IF NOT EXISTS images (
    sha     TEXT PRIMARY KEY,
    level   TEXT NOT NULL,
    regions TEXT NOT NULL,
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
        scale = 1.0
        try:
            import io as _io

            from PIL import Image

            with Image.open(_io.BytesIO(image_bytes)) as im:
                short = min(im.size)
                if 0 < short < 480:
                    scale = min(640 / short, 4.0)
                    resized = im.convert("RGB").resize(
                        (int(im.width * scale), int(im.height * scale)),
                        Image.Resampling.LANCZOS)
                    out = _io.BytesIO()
                    resized.save(out, "JPEG", quality=90)
                    image_bytes = out.getvalue()
        except Exception:  # noqa: BLE001 - detection on the original instead
            scale = 1.0

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


class ImageFilter:
    """Detector, cache and a deadline, wired together.

    Inference happens off the caller's thread with a hard timeout: a slow
    picture must not hold up a page. A picture that cannot be judged in
    time is treated as unjudged, and the caller hides it — the safe
    direction, and the one a person can understand ("pictures are off
    while this is busy" beats "sometimes it lets things through").
    """

    def __init__(self, detector=None, cache: VerdictCache | None = None,
                 timeout: float = DETECT_TIMEOUT, workers: int = 2,
                 slow_ms: float = SLOW_DETECT_MS):
        self.detector = detector if detector is not None else NudeNetDetector()
        self.cache = cache if cache is not None else VerdictCache()
        self.timeout = timeout
        self._pool = None
        self._workers = workers
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
            # that may have two cores.
            self._pool = ThreadPoolExecutor(max_workers=self._workers,
                                            thread_name_prefix="kosher-vision")
        return self._pool

    def verdict(self, image_bytes: bytes) -> ImageVerdict | None:
        """Judge one picture. None means it could not be judged."""
        if len(image_bytes) < MIN_IMAGE_BYTES:
            # Icons, spacers and tracking pixels: not worth the model's
            # time and not worth hiding either.
            return ImageVerdict(CLEAN, ())
        sha = digest(image_bytes)
        cached = self.cache.get(sha)
        if cached is not None:
            # Always served: a verdict already reached costs nothing and
            # is just as accurate on a slow machine as on a fast one.
            return cached
        if self.degraded:
            return None
        started = time.monotonic()
        future = self._executor().submit(self.detector.detect, image_bytes)
        try:
            detections = future.result(timeout=self.timeout)
        except Exception:  # noqa: BLE001 - includes the timeout
            future.cancel()
            self._record(self.timeout * 1000)
            return None
        self._record((time.monotonic() - started) * 1000)
        if detections is None:
            return None
        verdict = judge(detections)
        self.cache.put(sha, verdict)
        return verdict
