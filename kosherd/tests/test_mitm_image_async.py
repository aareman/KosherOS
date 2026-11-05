"""The whole image-response path must leave the proxy loop responsive."""

import asyncio
import threading

import pytest

from kosherd import vision
from test_mitm_addon import addon as addon, _filt, _hflow, _real_photo


async def started(event):
    async def poll():
        while not event.is_set():
            await asyncio.sleep(0.001)
    await asyncio.wait_for(poll(), 2)


@pytest.fixture
def make_filter(addon, monkeypatch):
    filters = []
    monkeypatch.setattr(addon.activity_mod, "record", lambda *a, **kw: None)

    def make(level="nsfw", verdict=None, workers=1):
        images = vision.ImageFilter(workers=workers, timeout=0.03)
        images.write_status = lambda: None
        async def known(_):
            return verdict
        images.verdict_async = known
        proxy = _filt(addon, media=level, vision=images)
        proxy.IMAGE_FINALIZE_TIMEOUT = 0.03
        proxy.IMAGE_COVER_TIMEOUT = 0.03
        proxy.IMAGE_PLACEHOLDER_TIMEOUT = 0.03
        filters.append(images)
        return proxy

    yield make
    for images in filters:
        images.close(wait=True)


@pytest.mark.parametrize("level", ["all", "nsfw", "preview"])
def test_hidden_picture_preparation_is_off_loop(addon, make_filter, monkeypatch, level):
    proxy = make_filter("none" if level == "preview" else level)
    body = _real_photo()
    flow = _hflow("image/jpeg", body=body)
    if level == "preview":
        flow.request.pretty_host = "i.ytimg.com"
        flow.request.path = "/an_webp/v1/mqdefault_6s.webp"
        proxy._youtube_limited = lambda uid: True
    entered, release = threading.Event(), threading.Event()
    loop_thread = threading.get_ident()
    proxy.IMAGE_PLACEHOLDER_TIMEOUT = 2

    def placeholder(_):
        assert threading.get_ident() != loop_thread
        entered.set()
        assert release.wait(2)
        return b"prepared placeholder"

    monkeypatch.setattr(addon.imageedit_mod, "placeholder_for", placeholder)
    async def run():
        task = asyncio.create_task(proxy._response(flow))
        try:
            await started(entered)
            assert await asyncio.sleep(0, result="responsive") == "responsive"
            assert flow.response.content == body, "workers must not mutate flows"
            release.set()
            await task
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
        assert flow.response.content == b"prepared placeholder"
        assert flow.response.headers["x-kosheros"] == "image-hidden"
    asyncio.run(run())


def test_cover_timeout_hides_without_submitting_stale_cover(addon, make_filter, monkeypatch):
    verdict = vision.ImageVerdict(vision.NSFW, ((120, 120, 40, 40),), True)
    proxy = make_filter(verdict=verdict)
    flow = _hflow("image/jpeg", body=_real_photo())
    entered, release = threading.Event(), threading.Event()
    calls = []
    monkeypatch.setattr(addon.imageedit_mod, "placeholder_for", lambda _: b"hidden")
    proxy._cover_or_hide = lambda *args: calls.append("cover")
    def occupy():
        entered.set()
        assert release.wait(2)
    async def run():
        busy = asyncio.create_task(proxy.vision.run_async(occupy))
        try:
            await started(entered)
            await asyncio.wait_for(proxy._filter_image_async(flow, 1001), 1)
            assert flow.response.content == b"hidden"
            assert not calls
        finally:
            release.set()
            await busy
        await proxy.vision.run_async(lambda: None)
        assert not calls, "expired covers never turn into work later"
    asyncio.run(run())


def test_busy_preparation_uses_safe_fallback_without_waiting_forever(addon, make_filter, monkeypatch):
    proxy = make_filter("all")
    flow = _hflow("image/jpeg", body=_real_photo())
    entered, release = threading.Event(), threading.Event()
    calls = []
    monkeypatch.setattr(addon.imageedit_mod, "placeholder_for", lambda _: calls.append("tile"))
    def occupy():
        entered.set()
        assert release.wait(2)
    async def run():
        busy = asyncio.create_task(proxy.vision.prepare_async(occupy))
        try:
            await started(entered)
            await asyncio.wait_for(proxy._filter_image_async(flow, 1001), 1)
            assert flow.response.content == addon.BLANK_PNG
        finally:
            release.set()
            await busy
        await proxy.vision.prepare_async(lambda: None)
        assert not calls
    asyncio.run(run())


def test_animation_headers_and_cover_are_checked_off_loop(addon, make_filter, monkeypatch):
    verdict = vision.ImageVerdict(vision.NSFW, ((120, 120, 40, 40),), True)
    proxy = make_filter(verdict=verdict)
    proxy.IMAGE_COVER_TIMEOUT = 2
    flow = _hflow("image/jpeg", body=_real_photo())
    loop_thread = threading.get_ident()
    checked = []
    def animated(_):
        assert threading.get_ident() != loop_thread
        checked.append("headers")
        return False
    def cover(*args):
        assert threading.get_ident() != loop_thread
        checked.append("cover")
        return b"\xff\xd8covered"
    monkeypatch.setattr(addon.imageedit_mod, "is_animated", animated)
    proxy._cover = cover
    asyncio.run(proxy._filter_image_async(flow, 1001))
    assert checked == ["headers", "cover"]
    assert flow.response.content == b"\xff\xd8covered"


def test_status_is_coalesced_and_does_not_gate_clean_images(make_filter):
    proxy = make_filter(verdict=vision.ImageVerdict(vision.CLEAN))
    body = _real_photo()
    entered, release = threading.Event(), threading.Event()
    calls = []
    def status():
        calls.append("status")
        entered.set()
        assert release.wait(2)
    proxy.vision.write_status = status
    async def run():
        try:
            await proxy._filter_image_async(_hflow("image/jpeg", body=body), 1001)
            await started(entered)
            pending = proxy._image_status_task
            for _ in range(5):
                flow = _hflow("image/jpeg", body=body)
                await proxy._filter_image_async(flow, 1001)
                assert flow.response.content == body
                assert proxy._image_status_task is pending
        finally:
            release.set()
            await proxy._image_status_task
        assert calls == ["status"]
    asyncio.run(run())


def test_placeholder_burst_preserves_dimensions_after_preparation_delay(addon, make_filter, monkeypatch):
    import io
    import time
    from PIL import Image

    proxy = make_filter("all")
    proxy.IMAGE_PLACEHOLDER_TIMEOUT = addon.KosherFilter.IMAGE_PLACEHOLDER_TIMEOUT
    bodies = []
    sizes = [(1600 + i, 1000 + i) for i in range(24)]
    for size in sizes:
        out = io.BytesIO()
        Image.new("RGB", size, (90, 120, 200)).save(out, "JPEG")
        bodies.append(out.getvalue())
    addon.imageedit_mod._placeholders.clear()
    placeholder = addon.imageedit_mod.placeholder_for
    first = True

    def delayed(body):
        nonlocal first
        if first:
            first = False
            # Real-model browser evidence exceeded the old two-second
            # placeholder deadline under CPU contention. Keep geometry
            # even when the preparation queue takes longer than that.
            time.sleep(2.2)
        return placeholder(body)

    monkeypatch.setattr(addon.imageedit_mod, "placeholder_for", delayed)
    flows = [_hflow("image/jpeg", body=bodies[i % 24]) for i in range(36)]
    async def run():
        await asyncio.gather(*(proxy._filter_image_async(flow, 1001) for flow in flows))
    asyncio.run(run())
    for i, flow in enumerate(flows):
        with Image.open(io.BytesIO(flow.response.content)) as image:
            assert image.size == sizes[i % 24]
            color = image.convert("RGB").getpixel((0, 0))
            assert all(abs(got - source) <= 16 for got, source in zip(color, (90, 120, 200)))


class EncodedResponse:
    """Guard against mitmproxy-style content decoding/encoding on the loop."""

    def __init__(self, raw):
        self.raw_content = raw
        self.headers = {"content-type": "image/jpeg", "content-encoding": "gzip",
                        "content-length": str(len(raw)), "etag": '"encoded"',
                        "content-digest": "stale digest"}
        self.status_code = 200

    @property
    def content(self):
        assert "content-encoding" not in self.headers, "synchronous decode"
        return self.raw_content

    @content.setter
    def content(self, value):
        assert "content-encoding" not in self.headers, "synchronous encode"
        self.raw_content = value


@pytest.mark.parametrize("hidden", [False, True])
def test_http_compressed_images_decode_off_loop_and_preserve_pixels(
        addon, make_filter, monkeypatch, hidden):
    import gzip
    import io
    from PIL import Image

    body = _real_photo()
    proxy = make_filter("all" if hidden else "nsfw", vision.ImageVerdict(vision.CLEAN))
    proxy.IMAGE_PLACEHOLDER_TIMEOUT = 2
    flow = _hflow("image/jpeg", body=body)
    flow.response = EncodedResponse(gzip.compress(body))
    loop_thread = threading.get_ident()
    calls = []

    def decode(raw, encoding):
        assert threading.get_ident() != loop_thread
        assert encoding == "gzip"
        calls.append("decode")
        return gzip.decompress(raw)

    monkeypatch.setattr(proxy, "_decode_media_bytes", decode)
    asyncio.run(proxy._response(flow))
    assert calls == ["decode"]
    assert "content-encoding" not in flow.response.headers
    assert "etag" not in flow.response.headers
    assert "content-digest" not in flow.response.headers
    if hidden:
        assert flow.response.headers["x-kosheros"] == "image-hidden"
        with Image.open(io.BytesIO(flow.response.content)) as image:
            assert image.size == (300, 300)
            rgb = image.convert("RGB")
            assert all(low == high for low, high in rgb.getextrema())
            assert rgb.getpixel((0, 0)) != addon.imageedit_mod.PLACEHOLDER_GREY
    else:
        assert flow.response.content == body
        assert flow.response.headers["content-length"] == str(len(body))


def test_invalid_http_compression_hides_without_reading_content(make_filter, monkeypatch, addon):
    import gzip

    proxy = make_filter("nsfw")
    flow = _hflow("image/jpeg")
    flow.response = EncodedResponse(b"not gzip")
    monkeypatch.setattr(proxy, "_decode_media_bytes", lambda raw, _: gzip.decompress(raw))
    asyncio.run(proxy._response(flow))
    assert flow.response.content == addon.BLANK_PNG
    assert flow.response.headers["x-kosheros"] == "image-hidden"
    assert "content-encoding" not in flow.response.headers
    assert "content-length" not in flow.response.headers
    assert "etag" not in flow.response.headers
    assert "content-digest" not in flow.response.headers


def test_unfiltered_media_keeps_original_http_encoding(make_filter):
    proxy = make_filter("none")
    flow = _hflow("image/jpeg")
    flow.response = EncodedResponse(b"opaque compressed bytes")
    asyncio.run(proxy._response(flow))
    assert flow.response.raw_content == b"opaque compressed bytes"
    assert flow.response.headers["content-encoding"] == "gzip"
    assert flow.response.headers["etag"] == '"encoded"'


def test_duplicate_covers_share_one_worker_and_reuse_prepared_key(addon, make_filter, monkeypatch):
    verdict = vision.ImageVerdict(vision.NSFW, ((120, 120, 40, 40),), True)
    proxy = make_filter(verdict=verdict, workers=2)
    proxy.IMAGE_COVER_TIMEOUT = 2
    proxy._publish_image_status = lambda: None
    body = _real_photo()
    entered, release = threading.Event(), threading.Event()
    rendered, hashes = [], []
    digest = addon.vision_mod.digest
    loop_thread = threading.get_ident()

    def hashed(data):
        assert threading.get_ident() != loop_thread
        hashes.append(data)
        return digest(data)

    def cover(data, regions, *, style):
        rendered.append(data)
        entered.set()
        assert release.wait(2)
        return b"\xff\xd8covered"

    monkeypatch.setattr(addon.vision_mod, "digest", hashed)
    monkeypatch.setattr(addon.imageedit_mod, "cover", cover)

    async def run():
        waiting = 0
        all_waiting = asyncio.Event()
        run_work = proxy.vision.run_async

        async def counted(*args, **kwargs):
            nonlocal waiting
            if kwargs.get("share_key", (None,))[0] == "cover":
                waiting += 1
                if waiting == 10:
                    all_waiting.set()
            return await run_work(*args, **kwargs)

        proxy.vision.run_async = counted
        flows = [_hflow("image/jpeg", body=body) for _ in range(10)]
        tasks = [asyncio.create_task(proxy._filter_image_async(flow, 1001)) for flow in flows]
        try:
            await started(entered)
            await asyncio.wait_for(all_waiting.wait(), 1)
            assert len(rendered) == 1, "duplicates must not occupy the other worker"
            release.set()
            await asyncio.gather(*tasks)
            assert all(flow.response.content == b"\xff\xd8covered" for flow in flows)
            assert len(hashes) == len(flows), "reuse the prepared key during rendering"
            assert len(proxy.covers) == 1
        finally:
            release.set()
            await asyncio.gather(*tasks, return_exceptions=True)
    asyncio.run(run())


def test_cover_burst_can_wait_beyond_the_decode_budget(make_filter):
    verdict = vision.ImageVerdict(vision.NSFW, ((120, 120, 40, 40),), True)
    proxy = make_filter(verdict=verdict)
    proxy.IMAGE_FINALIZE_TIMEOUT = 0.02
    proxy.IMAGE_COVER_TIMEOUT = 0.3
    proxy._publish_image_status = lambda: None
    proxy._cover_or_hide = lambda *args: b"\xff\xd8covered"
    flow = _hflow("image/jpeg", body=_real_photo())
    entered, release = threading.Event(), threading.Event()

    def occupy():
        entered.set()
        assert release.wait(2)

    async def run():
        busy = asyncio.create_task(proxy.vision.run_async(occupy))
        try:
            await started(entered)
            asyncio.get_running_loop().call_later(0.06, release.set)
            await asyncio.wait_for(proxy._filter_image_async(flow, 1001), 1)
            assert flow.response.content == b"\xff\xd8covered"
            assert flow.response.headers["x-kosheros"] == "image-covered=nsfw"
        finally:
            release.set()
            await busy
    asyncio.run(run())
