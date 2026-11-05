"""A busy image worker must leave the proxy responsive and no stale jobs."""

import asyncio
import threading

import pytest

from kosherd import videocheck, vision


class MemoryCache:
    def __init__(self):
        self.values = {}

    def get(self, key):
        return self.values.get(key)

    def put(self, key, value):
        self.values[key] = value


def image_filter(monkeypatch, timeout=0.05, workers=1):
    monkeypatch.setattr(vision, "looks_animated", lambda _: False)
    monkeypatch.setattr(vision, "animation_frames", lambda _: None)
    return vision.ImageFilter(cache=MemoryCache(), workers=workers, timeout=timeout)


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
            await asyncio.to_thread(filt.close, wait=True)

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
            await asyncio.to_thread(filt.close, wait=True)
        assert len(calls) == 1, "expired/cancelled waiters never become CPU work"
        assert filt.cache.get(vision.digest(b"a" * 3000)) is not None

    asyncio.run(run())


@pytest.mark.parametrize("workers", [1, 2])
def test_duplicate_images_share_one_running_inference(monkeypatch, workers):
    filt = image_filter(monkeypatch, timeout=2, workers=workers)
    started, release = threading.Event(), threading.Event()
    calls = []

    def judge(data):
        calls.append(data)
        started.set()
        assert release.wait(2)
        return vision.ImageVerdict(vision.CLEAN)

    filt._judge_one = judge

    async def run():
        admitted = 0
        all_waiting = asyncio.Event()
        run_work = filt.run_async

        async def counted(*args, **kwargs):
            nonlocal admitted
            admitted += 1
            if admitted == 10:
                all_waiting.set()
            return await run_work(*args, **kwargs)

        filt.run_async = counted
        tasks = [asyncio.create_task(filt.verdict_async(b"a" * 3000)) for _ in range(10)]
        try:
            await wait_started(started)
            await asyncio.wait_for(all_waiting.wait(), 1)
            assert len(calls) == 1, "duplicates must not occupy the second worker"
            release.set()
            assert all(v.level == vision.CLEAN for v in await asyncio.gather(*tasks))
            assert len(calls) == 1
        finally:
            release.set()
            await asyncio.gather(*tasks, return_exceptions=True)
            await asyncio.to_thread(filt.close, wait=True)

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
            await asyncio.to_thread(filt.close, wait=True)

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
            await asyncio.to_thread(filt.close, wait=True)
        assert not calls

    asyncio.run(run())


def test_cancelling_a_duplicate_does_not_cancel_shared_inference(monkeypatch):
    filt = image_filter(monkeypatch, timeout=2, workers=2)
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
        try:
            await wait_started(started)
            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first
            # The original caller is gone, but its actual thread still
            # owns both the worker slot and the shared result.
            joined = asyncio.Event()
            run_work = filt.run_async

            async def counted(*args, **kwargs):
                joined.set()
                return await run_work(*args, **kwargs)

            filt.run_async = counted
            duplicate = asyncio.create_task(filt.verdict_async(b"a" * 3000))
            await asyncio.wait_for(joined.wait(), 1)
            assert len(calls) == 1
            release.set()
            assert (await duplicate).level == vision.CLEAN
            assert len(calls) == 1
        finally:
            release.set()
            await asyncio.to_thread(filt.close, wait=True)
        assert not filt._inflight

    asyncio.run(run())


def test_late_worker_failure_after_cancellation_is_observed(monkeypatch):
    import gc

    filt = image_filter(monkeypatch)
    started, release = threading.Event(), threading.Event()

    def broken():
        started.set()
        assert release.wait(2)
        raise ValueError("worker failed after its caller left")

    async def run():
        errors = []
        loop = asyncio.get_running_loop()
        loop.set_exception_handler(lambda _loop, context: errors.append(context))
        first = asyncio.create_task(filt.run_async(broken, share_key="late"))
        try:
            await wait_started(started)
            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first
            release.set()
            assert await asyncio.wait_for(filt.run_async(lambda: 42), 1) == 42
            gc.collect()
            await asyncio.sleep(0)
            assert not errors, "abandoned results must not log unhandled exceptions"
            assert not filt._inflight
        finally:
            release.set()
            await asyncio.to_thread(filt.close, wait=True)

    asyncio.run(run())


def test_expired_preparation_waiters_never_run_and_pool_recovers(monkeypatch):
    filt = image_filter(monkeypatch)
    started, release = threading.Event(), threading.Event()
    calls = []

    def work(value):
        calls.append(value)
        started.set()
        assert release.wait(2)
        return value

    async def run():
        first = asyncio.create_task(filt.prepare_async(work, "first"))
        try:
            await wait_started(started)
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(filt.prepare_async(work, "expired"), .02)
            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first
            # Cancellation cannot free a slot still owned by a thread.
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(filt.prepare_async(work, "also expired"), .02)
            release.set()
            assert await asyncio.wait_for(filt.prepare_async(lambda: 42), 1) == 42
            assert calls == ["first"]
        finally:
            release.set()
            await asyncio.to_thread(filt.close, wait=True)

    asyncio.run(run())


def test_shared_failure_releases_key_for_a_retry(monkeypatch):
    filt = image_filter(monkeypatch)

    def broken():
        raise ValueError("bad image")

    async def run():
        try:
            with pytest.raises(ValueError):
                await filt.run_async(broken, share_key=("image", "key"))
            assert await filt.run_async(lambda: 42, share_key=("image", "key")) == 42
        finally:
            await asyncio.to_thread(filt.close, wait=True)
        assert not filt._inflight
        with pytest.raises(RuntimeError, match="closed"):
            await filt.prepare_async(lambda: 1)

    asyncio.run(run())
