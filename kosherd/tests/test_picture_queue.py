"""A burst of pictures waits its turn instead of being hidden (#322).

The judging deadline was counted from a picture's arrival, so on a page
that asked for many pictures at once, all but the first few ran out of
time in the queue and were hidden, though each needed a fraction of the
deadline. Waiting for a worker now has its own budget."""
import asyncio
import io
import threading
import time

import pytest

from kosherd import vision

Image = pytest.importorskip("PIL.Image")


def _picture(n):
    out = io.BytesIO()
    Image.new("RGB", (64, 64), (n, 100, 150)).save(out, "PNG")
    return out.getvalue() + bytes(vision.MIN_IMAGE_BYTES) + bytes([n])


class Slow:
    available = True

    def __init__(self, seconds):
        self.seconds, self.calls = seconds, 0
        self.lock = threading.Lock()

    def detect(self, image_bytes):
        with self.lock:
            self.calls += 1
        time.sleep(self.seconds)
        return []


def _checker(tmp_path, detector, *, timeout, queue_wait):
    checker = vision.ImageFilter(detector=detector, timeout=timeout, workers=1,
                                 cache=vision.VerdictCache(tmp_path / "v.sqlite"),
                                 slow_ms=60_000)
    checker.queue_wait = queue_wait
    return checker


async def _burst(checker, count):
    return await asyncio.gather(*(checker.verdict_async(_picture(n)) for n in range(count)))


def test_a_burst_longer_than_the_deadline_is_judged_not_hidden(tmp_path):
    # Eight pictures at 0.15 s each on one worker: 1.2 s of work, and a
    # deadline of 0.4 s per picture. Counted from arrival, the later ones
    # ran out in the queue.
    checker = _checker(tmp_path, Slow(0.15), timeout=0.4, queue_wait=5)
    verdicts = asyncio.run(_burst(checker, 8))
    assert all(v is not None and v.level == vision.CLEAN for v in verdicts)


def test_a_picture_that_waits_past_the_queue_budget_is_never_judged(tmp_path):
    detector = Slow(0.3)
    checker = _checker(tmp_path, detector, timeout=1.0, queue_wait=0.2)
    verdicts = asyncio.run(_burst(checker, 4))
    assert verdicts[0] is not None
    assert verdicts[1:] == [None, None, None]
    time.sleep(0.5)
    assert detector.calls == 1          # the ones that left the queue never ran


def test_judging_that_runs_past_its_deadline_still_gives_up(tmp_path):
    checker = _checker(tmp_path, Slow(0.5), timeout=0.1, queue_wait=5)
    assert asyncio.run(checker.verdict_async(_picture(1))) is None
