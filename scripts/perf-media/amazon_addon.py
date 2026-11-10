"""Public-site evidence addon: production media filtering, isolated test policy.

Never load in the system proxy. No account cookies/headers or browsing history
are recorded. HTML receives the production image-status UI; public media
samples are saved for matching model verdicts to visible images after the run.
"""
import asyncio
from contextvars import ContextVar
import hashlib
import html
import importlib.util
import json
import os
from pathlib import Path
import time
from types import SimpleNamespace

from mitmproxy import http

ROOT = Path(os.environ['AMAZON_PERF_ROOT'])
OUT = Path(os.environ['AMAZON_PERF_OUT'])
LEVEL = os.environ.get('AMAZON_PERF_LEVEL', 'immodest')
spec = importlib.util.spec_from_file_location('amazon_measured_filter', ROOT/'mitm/kosher_filter.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
module.activity_mod.record = lambda *args, **kwargs: None
from kosherd import imageedit, videocheck, vision  # noqa: E402

CURRENT = ContextVar('media_record', default=None)


class PublicSiteFilter(module.KosherFilter):
    def __init__(self):
        self.policy = SimpleNamespace(media_level_for=lambda _: LEVEL,
            cover_style_for=lambda _: 'frost', blocked_categories_for=lambda _: [],
            system_allowed_for=lambda _: (),
            youtube_for=lambda _: {'blocked_categories': ['fixture-preview-limit']})
        self.categories = SimpleNamespace(blocked_categories_of=lambda *_: set(),
                                          categories_of=lambda *_: set())
        self.page_levels = {}
        self.covers = imageedit.CoverCache()
        self.vision = vision.ImageFilter(cache=vision.VerdictCache(OUT/'verdicts.sqlite'))
        self.videos = videocheck.VideoChecker(self.vision)
        self.siterules = SimpleNamespace(is_suggestions=lambda _: False)
        self.usage = SimpleNamespace(add=lambda *a, **k: None, latency=lambda *a, **k: None,
                                     flush=lambda: None)
        self.vision.write_status = lambda: None
        from PIL import Image, features
        self.stats = {'pillow_version': Image.__version__, 'pillow_file': Image.__file__,
                      'avif_decoder': features.check('avif'), 'level': LEVEL, 'responses': [], 'loop_lag_ms': [], 'errors': [],
                      'source': str(ROOT), 'inferences': 0,
                      'html_status_ui': [], 'status_api_responses': []}
        detect = self.vision.detector.detect
        def counted(data):
            self.stats['inferences'] += 1
            return detect(data)
        self.vision.detector.detect = counted
        verdict_async = self.vision.verdict_async
        async def observed(data):
            result = await verdict_async(data)
            record = CURRENT.get()
            if record is not None:
                record['verdict'] = None if result is None else {
                    'level': result.level, 'has_person': result.has_person,
                    'regions': result.regions}
            return result
        self.vision.verdict_async = observed
        self.ticker = None
        self.pending = set()
        (OUT/'media').mkdir(exist_ok=True)

    async def running(self):
        # Warm model availability without caching any real site's verdicts.
        await self.vision.run_async(lambda: self.vision.available)
        self.ticker = asyncio.create_task(self.tick())
        (OUT/'ready').write_text('ready')

    async def tick(self):
        while True:
            start = time.perf_counter()
            await asyncio.sleep(.01)
            self.stats['loop_lag_ms'].append(max(0, (time.perf_counter()-start-.01)*1000))

    def request(self, flow):
        flow.metadata['kosher_uid'] = 1001
        if self._serve_page_filter(flow, 1001):
            if flow.request.path.split('?', 1)[0] == getattr(module, 'IMAGE_STATUS_PATH', None):
                self.stats['status_api_responses'].append({
                    'url': flow.request.pretty_url, 'status': flow.response.status_code,
                    'states': json.loads(flow.response.get_text()).get('images', {})
                    if flow.response.status_code == 200 else {}})
            return
        if flow.request.host == 'i.ytimg.com' and flow.request.path == '/an_webp/kosher-invalid-preview/mqdefault_6s.webp':
            # Explicitly synthetic malformed preview; exercise the real
            # production fallback without relying on a signed hover URL.
            flow.response = http.Response.make(200, b'invalid preview fixture',
                {'content-type': 'image/webp', 'cache-control': 'no-store'})
            flow.metadata['synthetic_invalid_preview'] = True
            return
        if flow.request.host == '127.0.0.1' and flow.request.path == '/__youtube_asset_check':
            urls = [('Public Big Buck Bunny thumbnail', 'https://i.ytimg.com/vi/aqz-KE-bpKQ/hqdefault.jpg'),
                    ('Public Rick Astley thumbnail', 'https://i.ytimg.com/vi/dQw4w9WgXc/hqdefault.jpg'),
                    ('Synthetic malformed hover preview (fallback test)', 'https://i.ytimg.com/an_webp/kosher-invalid-preview/mqdefault_6s.webp')]
            cards = ''.join('<figure><img src="' + html.escape(url, quote=True) + '" width="320" height="180"><figcaption>' + label + '</figcaption></figure>' for label,url in urls)
            body = ('<!doctype html><title>Public YouTube thumbnail and hover fallback check</title><style>body{font:18px sans-serif;background:white}figure{display:inline-block;margin:10px}img{object-fit:fill}figcaption{max-width:320px}</style><h1>YouTube thumbnails and malformed preview fallback</h1>' + cards).encode()
            flow.response = http.Response.make(200, body, {'content-type': 'text/html'})
            return
        if flow.request.host == '127.0.0.1' and flow.request.path == '/__prime_asset_check':
            samples = json.loads(Path(os.environ['AMAZON_PERF_ASSET_LIST']).read_text())[:4]
            cards = ''.join('<figure><img src="' + html.escape(sample['url'], quote=True) + '" width="720" height="270"><figcaption>' + html.escape(sample['url'].split('/sonata-images-prod/')[-1].split('/')[0]) + '</figcaption></figure>' for sample in samples)
            body = ('<!doctype html><title>Public Prime Video carousel image check</title><style>body{font:18px sans-serif;background:white}figure{display:inline-block;margin:10px}img{object-fit:contain}figcaption{max-width:720px;overflow-wrap:anywhere}</style><h1>Public Prime Video carousel images</h1>' + cards).encode()
            flow.response = http.Response.make(200, body, {'content-type': 'text/html'})
            return
        if flow.request.host == '127.0.0.1' and flow.request.path == '/__prime_background_check':
            sample = json.loads(Path(os.environ['AMAZON_PERF_ASSET_LIST']).read_text())[3]
            source = html.escape(sample['url'], quote=True)
            body = ('<!doctype html><title>Public Prime Video inline background check</title>'
                    '<style>body{font:18px sans-serif;background:white}</style>'
                    '<h1>Public Prime carousel image as an inline background</h1>'
                    '<div style="width:720px;height:270px;background-size:contain;'
                    'background-repeat:no-repeat;background-image:url(\'' + source + '\')"></div>'
                    '<p>Same public Law &amp; Order AVIF, displayed through CSS.</p>').encode()
            flow.response = http.Response.make(200, body, {'content-type': 'text/html'})
            return
        if flow.request.host == '127.0.0.1' and flow.request.path == '/__amazon_metrics':
            flow.response = http.Response.make(200, json.dumps(self.stats).encode(),
                                               {'content-type': 'application/json'})

    async def response(self, flow):
        if not (module._is_image(flow) or module._is_video(flow)):
            if 'text/html' in flow.response.headers.get('content-type', '').lower():
                upstream_script = module.SCRIPT_PATH in (flow.response.get_text(strict=False) or '')
                self._inject_page_script(flow, 'off', judge=False)
                self.stats['html_status_ui'].append({
                    'url': flow.request.pretty_url, 'upstream_script_present': upstream_script,
                    'image_status_enabled': 'data-kosheros-images="1"' in (flow.response.get_text(strict=False) or '')})
            return
        raw = flow.response.raw_content or b''
        record = {'url': flow.request.pretty_url,
                  'synthetic_invalid_preview': flow.metadata.get('synthetic_invalid_preview', False),
                  'input_mime': flow.response.headers.get('content-type', ''),
                  'input_encoding': flow.response.headers.get('content-encoding', ''),
                  'input_bytes': len(raw),
                  'upstream_filter_result': flow.response.headers.get('x-kosheros'),
                  'verdict': 'not-requested'}
        token = CURRENT.set(record)
        start = time.perf_counter()
        try:
            await super().response(flow)
        except Exception as error:
            record['error'] = repr(error)
            self.stats['errors'].append({'url': flow.request.pretty_url, 'error': repr(error)})
            raise
        finally:
            CURRENT.reset(token)
        record.update(elapsed_ms=(time.perf_counter()-start)*1000,
                      result=flow.response.headers.get('x-kosheros', 'passed'),
                      output_mime=flow.response.headers.get('content-type', ''),
                      output_bytes=len(flow.response.raw_content or b''),
                      status=flow.response.status_code)
        self.stats['responses'].append(record)
        output = flow.response.raw_content or b''
        # Evidence I/O is kept out of the response critical path. Store only
        # bounded public media bodies; never request headers or cookies.
        async def save():
            record.update(await asyncio.to_thread(self.save_media, raw, output))
        task = asyncio.create_task(save())
        self.pending.add(task)
        task.add_done_callback(self.pending.discard)

    @staticmethod
    def save_media(raw, output):
        record = {}
        for kind, body in (('input', raw), ('output', output)):
            record[kind+'_sha256'] = digest = hashlib.sha256(body).hexdigest()
            if body and len(body) <= 5*1024*1024:
                path = OUT/'media'/f'{digest}.{kind}.bin'
                if not path.exists():
                    path.write_bytes(body)
                record[kind+'_sample'] = str(path.relative_to(OUT))
        return record

    def error(self, flow):
        self.stats['errors'].append({'url': flow.request.pretty_url,
                                    'error': str(flow.error)})

    def _note_picture(self, flow):
        pass

    def _picture_note(self, flow):
        return None

    def done(self):
        (OUT/'proxy.json').write_text(json.dumps(self.stats, indent=2))
        if self.ticker:
            self.ticker.cancel()
        close = getattr(self.vision, 'close', None)
        if close:
            close()


addons = [PublicSiteFilter()]
