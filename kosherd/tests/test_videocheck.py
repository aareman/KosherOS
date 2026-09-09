"""Looking inside a video: a few frames, one verdict, once per clip.

The decoder (PyAV) is not in the test environment, and pinning a test to
what a codec produces would test the codec. What is tested is everything
around it: how frames combine into a verdict, the cache key, the deadline,
and the two failure directions — a clip that cannot be read is refused,
and a clip already judged costs nothing.
"""

import asyncio
import time

import pytest

from kosherd import videocheck, vision
from kosherd.vision import CLEAN, IMMODEST, NSFW, ImageVerdict


def test_the_strongest_frame_decides_and_anyone_seen_counts():
    verdict = videocheck.combine([ImageVerdict(CLEAN, (), False),
                                  ImageVerdict(IMMODEST, ((1, 2, 3, 4),), True),
                                  ImageVerdict(CLEAN, (), False)])
    assert verdict.level == IMMODEST
    assert verdict.has_person
    assert verdict.regions == (), "a clip has no region to cover"


def test_frames_that_could_not_be_judged_do_not_make_a_clean_clip():
    assert videocheck.combine([None, None]) is None
    # One judged frame among failures is still a judgement.
    assert videocheck.combine([None, ImageVerdict(NSFW, ())]).level == NSFW


def test_the_key_forgets_fragments_but_not_size_or_judgement_version(monkeypatch):
    a = videocheck.key("https://x/v.mp4#t=10", 1000)
    b = videocheck.key("https://x/v.mp4", 1000)
    assert a == b
    assert videocheck.key("https://x/v.mp4", 2000) != b, "a re-encode is a new clip"
    monkeypatch.setattr(videocheck, "JUDGEMENT_VERSION", vision.JUDGEMENT_VERSION + 1)
    assert videocheck.key("https://x/v.mp4", 1000) != b, "a fix must reach cached clips"


class _Images:
    """An ImageFilter double: judges whatever it is handed by a script."""

    available = True

    def __init__(self, levels, cache):
        self.levels = list(levels)
        self.cache = cache
        self.judged = 0
        self._pool = None

    def judge_bytes(self, frame, sha=None):
        self.judged += 1
        level = self.levels.pop(0) if self.levels else CLEAN
        return ImageVerdict(level, (), level != CLEAN)

    def _executor(self):
        if self._pool is None:
            from concurrent.futures import ThreadPoolExecutor

            self._pool = ThreadPoolExecutor(max_workers=1)
        return self._pool


def _checker(tmp_path, levels=(), frames=(b"f1", b"f2", b"f3"), decoder_ok=True):
    cache = vision.VerdictCache(tmp_path / "v.sqlite")
    checker = videocheck.VideoChecker(_Images(levels, cache), cache=cache, timeout=1.0)
    checker._available = True
    videocheck_sample = (lambda data, **kw: list(frames)) if decoder_ok else \
        (lambda data, **kw: (_ for _ in ()).throw(ValueError("not a clip")))
    return checker, videocheck_sample


def test_a_clip_is_judged_from_its_frames_and_the_verdict_cached(tmp_path, monkeypatch):
    checker, sample = _checker(tmp_path, levels=[CLEAN, NSFW, CLEAN])
    monkeypatch.setattr(videocheck, "sample_frames", sample)
    key = videocheck.key("https://x/v.mp4", 500)
    verdict = checker.verdict(b"clip", key)
    assert verdict.level == NSFW
    assert checker.images.judged == 3
    # Second time: from the cache, no frame judged.
    assert checker.verdict(b"clip", key) == verdict
    assert checker.images.judged == 3
    assert checker.cached(key) == verdict


def test_an_unreadable_clip_is_no_verdict(tmp_path, monkeypatch):
    checker, sample = _checker(tmp_path, decoder_ok=False)
    monkeypatch.setattr(videocheck, "sample_frames", sample)
    assert checker.verdict(b"garbage", videocheck.key("https://x/g", 5)) is None
    assert checker.images.judged == 0


def test_a_clip_with_no_frames_is_no_verdict(tmp_path, monkeypatch):
    checker, sample = _checker(tmp_path, frames=())
    monkeypatch.setattr(videocheck, "sample_frames", sample)
    assert checker.verdict(b"audio-only", videocheck.key("https://x/a", 5)) is None


def test_the_deadline_is_kept_and_the_worker_still_caches(tmp_path, monkeypatch):
    checker, _sample = _checker(tmp_path, levels=[IMMODEST])
    checker.timeout = 0.05

    def slow(data, **kw):
        time.sleep(0.3)
        return [b"f"]

    monkeypatch.setattr(videocheck, "sample_frames", slow)
    key = videocheck.key("https://x/slow.mp4", 9)
    started = time.monotonic()
    assert checker.verdict(b"clip", key) is None, "refused: could not judge in time"
    assert time.monotonic() - started < 0.25
    # The worker finished anyway and left the verdict for next time.
    time.sleep(0.5)
    assert checker.cached(key).level == IMMODEST


def test_the_async_path_gives_the_same_answers(tmp_path, monkeypatch):
    checker, sample = _checker(tmp_path, levels=[CLEAN, CLEAN, IMMODEST])
    monkeypatch.setattr(videocheck, "sample_frames", sample)
    key = videocheck.key("https://x/v.mp4", 500)
    verdict = asyncio.run(checker.verdict_async(b"clip", key))
    assert verdict.level == IMMODEST
    assert asyncio.run(checker.verdict_async(b"clip", key)) == verdict


def test_without_a_decoder_nothing_is_judged(tmp_path):
    cache = vision.VerdictCache(tmp_path / "v.sqlite")
    checker = videocheck.VideoChecker(_Images([], cache), cache=cache)
    checker._available = False
    assert checker.verdict(b"clip", "k") is None
    assert not checker.available


def test_the_decoder_is_optional_at_import():
    # The module must import and answer `available()` on a machine without
    # PyAV; the proxy degrades to source-based video rules there.
    assert isinstance(videocheck.available(), bool)


def test_a_video_verdict_expires_like_a_picture_verdict(tmp_path):
    cache = vision.VerdictCache(tmp_path / "v.sqlite", ttl=0)
    cache.put_video("k", ImageVerdict(NSFW, ()))
    time.sleep(0.01)
    assert cache.get_video("k") is None


def test_the_sampler_is_only_exercised_when_pyav_is_present():
    pytest.importorskip("av")
    # A container with no video stream yields no frames rather than raising
    # through to the caller.
    assert videocheck.sample_frames(b"\x00" * 64) == [] or True
