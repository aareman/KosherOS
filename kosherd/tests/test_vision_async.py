"""A busy image worker must leave the proxy responsive and no stale jobs."""

import asyncio
import threading

from kosherd import videocheck, vision


class MemoryCache:
    def __init__(self):
        self.values = {}

    def get(self, key):
        return self.values.get(key)

    def put(self, key, value):
        self.values[key] = value


def image_filter(monkeypatch, timeout=0.05):
    monkeypatch.setattr(vision, "looks_animated", lambda _: False)
    monkeypatch.setattr(vision, "animation_frames", lambda _: None)
    return vision.ImageFilter(cache=MemoryCache(), workers=1, timeout=timeout)


async def wait_started(event):
    async def poll():
        while not event.is_set():
            await asyncio.sleep(0.001)
    await asyncio.wait_for(poll(), 2)


def test_slow_cache_preparation_leaves_the_event_loop_responsive(monkeypatch):
    filt = image_filter(monkeypatch, timeout=2)
    started, release = threading.Event(), threading.Event()
    loop_thread = threading.get_ident()

    def slow_cache(data):
        assert threading.get_ident() != loop_thread
        started.set()
        assert release.wait(2)
        return vision.ImageVerdict(vision.CLEAN), None

    filt._prejudge = slow_cache

    async def run():
        task = asyncio.create_task(filt.verdict_async(b"a" * 3000))
        try:
            await wait_started(started)
            assert await asyncio.wait_for(asyncio.sleep(0, result="responsive"), 0.1) == "responsive"
            assert not task.done()
            release.set()
            assert (await task).level == vision.CLEAN
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
            await asyncio.to_thread(filt._prepare_pool.shutdown, wait=True)

    asyncio.run(run())


def test_timed_out_burst_never_runs_queued_images(monkeypatch):
    filt = image_filter(monkeypatch)
    started, release = threading.Event(), threading.Event()
    calls = []

    def judge(data):
        calls.append(data)
        started.set()
        assert release.wait(2)
        return vision.ImageVerdict(vision.CLEAN)

    filt._judge_one = judge

    async def run():
        first = asyncio.create_task(filt.verdict_async(b"a" * 3000))
        await wait_started(started)
        try:
            # Covers use the same worker admission as inference. Even
            # after first times out, its running thread still owns a slot.
            cover = asyncio.create_task(filt.run_async(lambda: calls.append(b"cover")))
            await asyncio.sleep(0)
            cover.cancel()
            try:
                await cover
            except asyncio.CancelledError:
                pass
            results = await asyncio.gather(first, *(filt.verdict_async(
                bytes([i]) * 3000) for i in range(20)))
            assert all(result is None for result in results)
            assert len(calls) == 1
            assert not filt.degraded, "queue wait is not detector latency"
            # Cached answers need no worker, even while inference is busy.
            cached_body = b"cached" * 600
            known = vision.ImageVerdict(vision.CLEAN)
            filt.cache.put(vision.digest(cached_body), known)
            assert await filt.verdict_async(cached_body) == known
        finally:
            release.set()
            await asyncio.to_thread(filt._pool.shutdown, wait=True)
        assert len(calls) == 1, "expired/cancelled waiters never become CPU work"
        assert filt.cache.get(vision.digest(b"a" * 3000)) is not None

    asyncio.run(run())


def test_duplicate_images_share_the_verdict_after_waiting(monkeypatch):
    filt = image_filter(monkeypatch, timeout=2)
    started, release = threading.Event(), threading.Event()
    calls = []

    def judge(data):
        calls.append(data)
        started.set()
        assert release.wait(2)
        return vision.ImageVerdict(vision.CLEAN)

    filt._judge_one = judge

    async def run():
        tasks = [asyncio.create_task(filt.verdict_async(b"a" * 3000)) for _ in range(10)]
        await wait_started(started)
        # Let the other requests reach admission before releasing worker.
        await asyncio.sleep(0)
        release.set()
        try:
            assert all(v.level == vision.CLEAN for v in await asyncio.gather(*tasks))
            assert len(calls) == 1
        finally:
            await asyncio.to_thread(filt._pool.shutdown, wait=True)

    asyncio.run(run())


def test_failed_work_releases_the_slot(monkeypatch):
    filt = image_filter(monkeypatch)

    def broken():
        raise ValueError("bad image")

    async def run():
        try:
            try:
                await filt.run_async(broken)
            except ValueError:
                pass
            assert await asyncio.wait_for(filt.run_async(lambda: 42), 1) == 42
        finally:
            await asyncio.to_thread(filt._pool.shutdown, wait=True)

    asyncio.run(run())


def test_video_uses_the_same_admission_and_never_runs_after_timeout(monkeypatch):
    filt = image_filter(monkeypatch)
    checker = videocheck.VideoChecker(filt, timeout=0.05)
    checker._available = True
    checker.cached = lambda _: None
    started, release = threading.Event(), threading.Event()
    calls = []

    def occupy_worker():
        started.set()
        assert release.wait(2)

    checker.judge_bytes = lambda *args: calls.append("video")

    async def run():
        occupied = asyncio.create_task(filt.run_async(occupy_worker))
        await wait_started(started)
        try:
            assert await checker.verdict_async(b"clip", "video-key") is None
        finally:
            release.set()
            await occupied
            await asyncio.to_thread(filt._pool.shutdown, wait=True)
        assert not calls

    asyncio.run(run())
