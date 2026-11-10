"""Cache contention and decoder startup must not hold the network loop."""

import asyncio
import threading
from types import SimpleNamespace

import pytest

from kosherd import videocheck, vision
from test_mitm_addon import addon, _filt, _hflow  # noqa: F401


async def wait_started(event):
    async def poll():
        while not event.is_set():
            await asyncio.sleep(0.001)
    await asyncio.wait_for(poll(), 2)


@pytest.mark.parametrize("method", ["precheck", "verdict", "remote"])
def test_cache_wait_has_a_deadline_and_leaves_the_loop_free(tmp_path, monkeypatch, method):
    monkeypatch.setattr(videocheck, "REMOTE_TIMEOUT", 0.05)
    cache = vision.VerdictCache(tmp_path / "cache.sqlite")
    images = vision.ImageFilter(cache=cache, workers=1, timeout=0.05)
    checker = videocheck.VideoChecker(images, timeout=0.05)
    started, release = threading.Event(), threading.Event()
    loop_thread = threading.get_ident()
    judged = []

    def blocked_cache(key):
        assert threading.get_ident() != loop_thread
        started.set()
        assert release.wait(2)
        return None

    checker.cached = blocked_cache
    checker._available = True
    checker.judge_bytes = lambda *args: judged.append(True)
    checker.judge_remote = lambda *args: judged.append(True)

    async def run():
        work = {
            "precheck": lambda: checker.precheck_async("clip"),
            "verdict": lambda: checker.verdict_async(b"clip", "clip"),
            "remote": lambda: checker.verdict_remote_async(lambda *args: b"", 100, "clip"),
        }[method]()
        task = asyncio.create_task(work)
        try:
            await wait_started(started)
            assert not task.done()
            assert await asyncio.wait_for(asyncio.sleep(0, result="responsive"), 0.1) == "responsive"
            assert await task == ((None, False) if method == "precheck" else None)
            assert not judged, "an expired cache waiter never starts inference"
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
            await asyncio.to_thread(images.close, wait=True)

    asyncio.run(run())


def test_lazy_decoder_discovery_runs_off_loop_without_holding_preparation(tmp_path, monkeypatch):
    images = vision.ImageFilter(cache=vision.VerdictCache(tmp_path / "cache.sqlite"),
                                detector=SimpleNamespace(available=True), timeout=2)
    checker = videocheck.VideoChecker(images)
    started, release = threading.Event(), threading.Event()
    loop_thread = threading.get_ident()

    def discover():
        assert threading.get_ident() != loop_thread
        started.set()
        assert release.wait(2)
        return True

    monkeypatch.setattr(videocheck, "available", discover)

    async def run():
        task = asyncio.create_task(checker.precheck_async("clip"))
        try:
            await wait_started(started)
            await asyncio.sleep(0)
            assert not task.done()
            assert await asyncio.wait_for(images.prepare_async(lambda: "cache free"), 0.1) == "cache free"
            release.set()
            assert await task == (None, True)
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
            await asyncio.to_thread(images.close, wait=True)

    asyncio.run(run())


@pytest.mark.parametrize("level,refused", [("immodest", True), ("nsfw", False)])
def test_response_headers_do_not_block_on_a_busy_cache(addon, tmp_path, level, refused):
    images = vision.ImageFilter(cache=vision.VerdictCache(tmp_path / "cache.sqlite"),
                                timeout=0.05)
    checker = videocheck.VideoChecker(images)
    checker._available = True
    proxy = _filt(addon, media=level, videos=checker, vision=images)
    proxy._note = lambda *a, **kw: None
    video = _hflow("video/mp4", length=1000)
    script = _hflow("application/javascript", body=b"console.log('ready')")
    started, release = threading.Event(), threading.Event()

    def blocked_cache(key):
        started.set()
        assert release.wait(2)
        return None

    checker.cached = blocked_cache

    async def run():
        task = asyncio.create_task(proxy.responseheaders(video))
        try:
            await wait_started(started)
            await asyncio.wait_for(proxy.responseheaders(script), 0.1)
            assert "kosher_filter_s" in script.metadata
            assert not task.done(), "other response headers progress while video waits"
            await task
            if refused:
                assert video.response.status_code == 403
                assert callable(video.response.stream)
            else:
                assert video.response.stream is True
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
            await asyncio.to_thread(images.close, wait=True)

    asyncio.run(run())


@pytest.mark.parametrize("remote", [False, True])
def test_concurrent_copies_of_a_video_share_inference(tmp_path, monkeypatch, remote):
    images = vision.ImageFilter(cache=vision.VerdictCache(tmp_path / "cache.sqlite"),
                                workers=2, timeout=2)
    checker = videocheck.VideoChecker(images, timeout=2)
    checker._available = True
    started, release = threading.Event(), threading.Event()
    calls = []

    def sample(data):
        calls.append(data)
        started.set()
        assert release.wait(2)
        return []  # header-only clip: its verdict still gets cached

    monkeypatch.setattr(videocheck, "sample_frames", sample)
    monkeypatch.setattr(videocheck, "sample_remote", lambda fetch, size: sample(size))

    async def run():
        tasks = [asyncio.create_task(
            checker.verdict_remote_async(lambda *args: b"", 100, "clip") if remote else
            checker.verdict_async(b"clip", "clip")) for _ in range(2)]
        try:
            await wait_started(started)
            await asyncio.sleep(0.02)
            release.set()
            assert all(value.level == vision.CLEAN for value in await asyncio.gather(*tasks))
            assert len(calls) == 1
        finally:
            release.set()
            await asyncio.gather(*tasks, return_exceptions=True)
            await asyncio.to_thread(images.close, wait=True)

    asyncio.run(run())


def test_distinct_ranges_of_one_video_are_judged_and_cached_separately(addon, tmp_path, monkeypatch):
    images = vision.ImageFilter(cache=vision.VerdictCache(tmp_path / "cache.sqlite"),
                                workers=2, timeout=2)
    checker = videocheck.VideoChecker(images, timeout=2)
    checker._available = True
    proxy = _filt(addon, media="immodest", videos=checker, vision=images)
    init = _hflow("video/mp4", status=206, content_range="bytes 0-99/1000", body=b"init")
    media = _hflow("video/mp4", status=206, content_range="bytes 100-999/1000", body=b"media")
    started = {b"init": threading.Event(), b"media": threading.Event()}
    release = threading.Event()

    def sample(data):
        started[data].set()
        assert release.wait(2)
        return [] if data == b"init" else [b"explicit frame"]

    monkeypatch.setattr(videocheck, "sample_frames", sample)
    monkeypatch.setattr(videocheck, "sample_remote", lambda fetch, size: sample(b"media"))
    monkeypatch.setattr(images, "judge_bytes", lambda data: vision.ImageVerdict(vision.NSFW))
    monkeypatch.setattr(addon.activity_mod, "record", lambda *args, **kwargs: None)

    async def run():
        tasks = [asyncio.create_task(proxy._filter_video_async(flow, 1001))
                 for flow in (init, media)]
        try:
            await wait_started(started[b"init"])
            await wait_started(started[b"media"])
            release.set()
            await asyncio.gather(*tasks)
            assert init.response.headers["x-kosheros"] == "video-checked=clean"
            assert media.response.status_code == 403
            # Repeat header decisions must find only their own cached verdict.
            same_init = _hflow("video/mp4", status=206, content_range="bytes 0-99/1000")
            same_media = _hflow("video/mp4", status=206, content_range="bytes 100-999/1000")
            extended = _hflow("video/mp4", status=206, content_range="bytes 0-499/1000")
            assert await proxy._video_decision_async(same_init, 1001) == "stream"
            assert await proxy._video_decision_async(same_media, 1001) == "refuse"
            assert await proxy._video_decision_async(extended, 1001) == "hold"
        finally:
            release.set()
            await asyncio.gather(*tasks, return_exceptions=True)
            await asyncio.to_thread(images.close, wait=True)

    asyncio.run(run())


@pytest.mark.parametrize("headers", [False, True])
def test_refusal_log_does_not_block_and_keeps_a_deadline(addon, tmp_path, monkeypatch, headers):
    images = vision.ImageFilter(cache=vision.VerdictCache(tmp_path / "cache.sqlite"),
                                timeout=1)
    proxy = _filt(addon, media="all", vision=images)
    proxy.IMAGE_ADVISORY_TIMEOUT = 0.05
    flow = _hflow("video/mp4", length=1000)
    started, release = threading.Event(), threading.Event()
    loop_thread = threading.get_ident()
    logged = []
    counts = []
    proxy.usage = SimpleNamespace(add=lambda uid, **values: counts.append((uid, values)))

    def slow_log(*args, **fields):
        assert threading.get_ident() != loop_thread
        logged.append((args, fields))
        started.set()
        assert release.wait(2)

    monkeypatch.setattr(addon.activity_mod, "record", slow_log)

    async def run():
        task = asyncio.create_task(proxy.responseheaders(flow) if headers else
                                   proxy._video_settle_async(flow, 1001, "all", None))
        try:
            await wait_started(started)
            await asyncio.wait_for(asyncio.sleep(0), 0.1)
            assert flow.response.status_code == 403
            assert not task.done(), "logging is running away from the event loop"
            await asyncio.wait_for(task, 0.5)
            assert not release.is_set(), "refusal does not wait for advisory disk I/O"
            assert counts == [(1001, {"videos": 1})]
            assert logged == [(("proxy", addon.activity_mod.VIDEO, 1001),
                               {"url": flow.request.pretty_url})]
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
            await asyncio.to_thread(images.close, wait=True)

    asyncio.run(run())
