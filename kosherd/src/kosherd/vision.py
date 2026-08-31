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
import logging
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

CACHE_PATH = Path("/var/lib/kosher-mitm/images.sqlite")

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
FEMALE_LABELS = frozenset({"FEMALE_FACE", "FEMALE_BREAST_COVERED",
                           "FEMALE_BREAST_EXPOSED", "FEMALE_GENITALIA_COVERED",
                           "FEMALE_GENITALIA_EXPOSED"})

# Below this the detector is guessing. Deliberately low: a missed explicit
# region costs far more than a blurred elbow.
MIN_CONFIDENCE = 0.25
# A picture smaller than this is an icon, a spacer or a tracking pixel.
MIN_IMAGE_BYTES = 6000
# Inference must not stall the page.
DETECT_TIMEOUT = 2.0
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
    if level == CLEAN:
        return ImageVerdict(CLEAN, ())
    covered = NSFW_LABELS | SUGGESTIVE_LABELS | IMMODEST_LABELS
    regions = tuple(d.box for d in kept if d.label in covered)
    return ImageVerdict(level, regions)


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
    seen    INTEGER NOT NULL
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
                    "SELECT level, regions, seen FROM images WHERE sha = ?",
                    (sha,)).fetchone()
        except sqlite3.Error:
            return None
        if not row or time.time() - row[2] > self.ttl:
            return None
        return ImageVerdict(row[0], _unpack(row[1]))

    def put(self, sha: str, verdict: ImageVerdict) -> None:
        try:
            with self._lock:
                db = self._conn()
                db.execute(
                    "INSERT INTO images (sha, level, regions, seen) "
                    "VALUES (?, ?, ?, ?) ON CONFLICT(sha) DO UPDATE SET "
                    "level=excluded.level, regions=excluded.regions, "
                    "seen=excluded.seen",
                    (sha, verdict.level, _pack(verdict.regions), int(time.time())))
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
                    box=(int(box[0]), int(box[1]), int(box[2]), int(box[3]))))
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
                 timeout: float = DETECT_TIMEOUT, workers: int = 2):
        self.detector = detector if detector is not None else NudeNetDetector()
        self.cache = cache if cache is not None else VerdictCache()
        self.timeout = timeout
        self._pool = None
        self._workers = workers

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
            return cached
        future = self._executor().submit(self.detector.detect, image_bytes)
        try:
            detections = future.result(timeout=self.timeout)
        except Exception:  # noqa: BLE001 - includes the timeout
            future.cancel()
            return None
        if detections is None:
            return None
        verdict = judge(detections)
        self.cache.put(sha, verdict)
        return verdict
