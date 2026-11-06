"""Isolated local-browser fixture; imports the selected checkout's real filter.

Only authentication, site policy and accounting are fixture-specific. Image and
video response hooks, caches, transforms and inference are the production code.
Never load this addon in the system proxy.
"""
import asyncio
import importlib.util
import json
import os
from pathlib import Path
import time
from types import SimpleNamespace

ROOT = Path(os.environ['MEDIA_PERF_ROOT'])
OUT = Path(os.environ['MEDIA_PERF_OUT'])
ASSETS = Path(os.environ['MEDIA_PERF_ASSETS'])
SCENARIO = os.environ['MEDIA_PERF_SCENARIO']
spec = importlib.util.spec_from_file_location('measured_filter', ROOT / 'mitm/kosher_filter.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
module.activity_mod.record = lambda *args, **kwargs: None
from kosherd import imageedit, videocheck, vision  # noqa: E402


class FixtureFilter(module.KosherFilter):
    def __init__(self):
        self.policy = SimpleNamespace(
            media_level_for=lambda _: 'all' if SCENARIO in ('hidden', 'intrinsic') else 'immodest',
            cover_style_for=lambda _: 'frost',
            blocked_categories_for=lambda _: [],
            system_allowed_for=lambda _: (),
        )
        self.categories = SimpleNamespace(blocked_categories_of=lambda *_: set(),
                                          categories_of=lambda *_: set())
        self.page_levels = {}
        self.covers = imageedit.CoverCache()
        self.vision = vision.ImageFilter(cache=vision.VerdictCache(OUT / 'verdicts.sqlite'))
        self.videos = videocheck.VideoChecker(self.vision)
        self.siterules = SimpleNamespace(is_suggestions=lambda _: False)
        self.usage = SimpleNamespace(add=lambda *a, **k: None, latency=lambda *a, **k: None,
                                     flush=lambda: None)
        self.vision.write_status = lambda: None
        self.stats = {'scenario': SCENARIO, 'inferences': 0, 'responses': [], 'loop_lag_ms': [],
                      'image_status_supported': hasattr(module, 'IMAGE_STATUS_PATH'),
                      'status_api_http_codes': []}
        detect = self.vision.detector.detect

        def counted(data):
            self.stats['inferences'] += 1
            return detect(data)

        self.vision.detector.detect = counted
        self.ticker = None
        if SCENARIO == 'covers':
            for path in ASSETS.glob('*.jpg'):
                # Seed a deterministic verdict solely to exercise cover rendering.
                self.vision.cache.put(vision.digest(path.read_bytes()),
                    vision.ImageVerdict(vision.NSFW, ((250, 200, 110, 100),), True))

    async def running(self):
        if SCENARIO in ('inference', 'covers'):
            # Exclude one-time model loading from steady-state measurements.
            await self.vision.run_async(self.vision.judge_bytes,
                                        (ASSETS / '0.jpg').read_bytes())
            if SCENARIO == 'inference':
                with self.vision.cache._lock:
                    self.vision.cache._conn().execute('DELETE FROM images')
                    self.vision.cache._conn().commit()
            else:
                self.vision.cache.put(vision.digest((ASSETS / '0.jpg').read_bytes()),
                    vision.ImageVerdict(vision.NSFW, ((250, 200, 110, 100),), True))
            if SCENARIO == 'inference':
                await self.vision.run_async(lambda: self.videos.available)
            self.stats['inferences'] = 0
        self.ticker = asyncio.create_task(self.tick())
        (OUT / 'ready').write_text('ready')

    async def tick(self):
        while True:
            start = time.perf_counter()
            await asyncio.sleep(.01)
            self.stats['loop_lag_ms'].append(max(0, (time.perf_counter()-start-.01)*1000))

    def request(self, flow):
        flow.metadata['kosher_uid'] = 1001
        if self._serve_page_filter(flow, 1001):
            if flow.request.path.split('?', 1)[0] == getattr(module, 'IMAGE_STATUS_PATH', None):
                self.stats['status_api_http_codes'].append(flow.response.status_code)
            return
        if flow.request.path == '/__metrics':
            from mitmproxy import http
            flow.response = http.Response.make(200, json.dumps(self.stats).encode(),
                                               {'content-type': 'application/json'})

    async def response(self, flow):
        if flow.request.path.startswith(('/image/', '/clip/')):
            start = time.perf_counter()
            await super().response(flow)
            self.stats['responses'].append({
                'path': flow.request.path, 'elapsed_ms': (time.perf_counter()-start)*1000,
                'result': flow.response.headers.get('x-kosheros', 'passed'),
                'status': flow.response.status_code})
        elif 'text/html' in flow.response.headers.get('content-type', '').lower():
            # Include the selected checkout's production status UI overhead.
            # Older baselines naturally skip images-only script injection.
            self._inject_page_script(flow, 'off', judge=False)

    def _note_picture(self, flow):
        pass  # The fixture must never write the real account's activity log.

    def _picture_note(self, flow):
        return None

    def done(self):
        (OUT / 'proxy.json').write_text(json.dumps(self.stats, indent=2))
        if self.ticker:
            self.ticker.cancel()


addons = [FixtureFilter()]
