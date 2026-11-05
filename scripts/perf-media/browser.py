#!/usr/bin/env python3
"""Measure an actual Chromium browser through the real local filtering addon.

Requires playwright, Pillow, PyAV, mitmproxy and the installed image models.
Connects ONLY to a separately launched browser profile at --cdp. The browser
must use --proxy-server=http://127.0.0.1:8087 --proxy-bypass-list=<-loopback>.
The HTTP fixture is local and synthetic; no private browsing data is accessed.
"""
import argparse
import asyncio
import base64
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hashlib
import json
import os
from pathlib import Path
import platform
import random
import signal
import subprocess
import threading
import time
from urllib.parse import urlsplit

from PIL import Image
from playwright.async_api import async_playwright

HERE = Path(__file__).resolve().parent

# A settled approved badge is deliberately hidden. Remember whether layout
# positioned it while pending so correctness still covers images that were
# observed, without demanding status for never-visible lazy content.
IMAGE_STATUS_SNAPSHOT = '''() => ({hosts:document.querySelectorAll('[data-kosheros-ui="images"]').length,badges:[...document.querySelectorAll('[data-kosheros-ui="images"]')].flatMap(host=>[...(host.shadowRoot?.querySelectorAll('.badge')||[])].map(b=>({state:b.getAttribute('data-state'),label:b.textContent,visible:!b.hidden&&b.getClientRects().length>0,was_displayed:!!b.style.transform,title:b.title,accessible_label:b.getAttribute('aria-label')})))})'''


def percentile(values, fraction):
    return round(sorted(values)[int((len(values)-1)*fraction)], 2) if values else None


async def capture_screenshot(session, path):
    """Capture after measurements, without Playwright's screenshot preflight."""
    try:
        result = await asyncio.wait_for(session.send('Page.captureScreenshot', {
            'format': 'png', 'fromSurface': True, 'captureBeyondViewport': False}), 10)
        path.write_bytes(base64.b64decode(result['data'], validate=True))
        return None
    except Exception as error:
        return {'path': path.name, 'error': type(error).__name__ + ': ' + str(error)}


def assets(root):
    root.mkdir(parents=True, exist_ok=True)
    rng = random.Random(42)
    for i in range(24):
        # Unique dimensions expose cold placeholder cost; repeated URLs with
        # identical bytes also exercise shared in-progress inference.
        size = (1600 + i * 4, 1000 + i * 3)
        im = Image.frombytes('RGB', size, rng.randbytes(size[0]*size[1]*3))
        im.save(root / f'{i}.jpg', quality=75)
    import av
    # H.264 is browser-decodable; MPEG-4 Part 2 would exercise only an
    # unsupported-video error in Chromium even if PyAV can sample it.
    with av.open(str(root / 'clip.mp4'), 'w', options={'movflags': '+faststart'}) as out:
        stream = out.add_stream('libx264', rate=12)
        stream.width, stream.height, stream.pix_fmt = 640, 360, 'yuv420p'
        for i in range(24):
            frame = av.VideoFrame.from_image(Image.new('RGB', (640, 360), (30+i*4, 80, 150)))
            for packet in stream.encode(frame):
                out.mux(packet)
        for packet in stream.encode():
            out.mux(packet)


PAGE = r'''<!doctype html><html><head><meta charset="utf-8"><title>Local media filtering benchmark</title>
<style>body{font:20px sans-serif;margin:80px 24px}button{position:fixed;left:16px;top:12px;width:180px;height:44px;z-index:9}img{width:240px;height:160px;object-fit:cover;background:#ddd}section{display:grid;grid-template-columns:repeat(4,240px);gap:12px}.intrinsic img{width:auto;height:auto;max-width:240px}#motion{position:fixed;right:10px;top:10px;animation:pulse 1s infinite alternate}@keyframes pulse{to{transform:translateX(-100px)}}</style>
<script>
window.metrics={frames:[],longtasks:[],inputs:[],events:[],pings:[],paints:[],lcp:[],shifts:[],errors:[]};
let last=performance.now();function frame(t){metrics.frames.push({time:t,gap_ms:t-last});last=t;requestAnimationFrame(frame)}requestAnimationFrame(frame);
for(const type of ['longtask','paint','largest-contentful-paint','event','layout-shift'])try{new PerformanceObserver(list=>{for(const e of list.getEntries()){if(type==='longtask')metrics.longtasks.push({time:e.startTime,duration:e.duration});else if(type==='event')metrics.events.push({name:e.name,time:e.startTime,duration:e.duration,processingStart:e.processingStart,processingEnd:e.processingEnd,interactionId:e.interactionId});else if(type==='layout-shift')metrics.shifts.push({time:e.startTime,value:e.value,hadRecentInput:e.hadRecentInput});else if(type==='paint')metrics.paints.push({name:e.name,time:e.startTime});else metrics.lcp.push(e.startTime)}}).observe({type,buffered:true,durationThreshold:16})}catch(e){}
setInterval(async()=>{const t=performance.now();try{await fetch('/ping?'+t,{cache:'no-store'});metrics.pings.push(performance.now()-t)}catch(e){metrics.errors.push(String(e))}},150);
function clickMe(e){const t=performance.now();document.getElementById('clicks').textContent=Number(document.getElementById('clicks').textContent)+1;requestAnimationFrame(()=>metrics.inputs.push({time:e.timeStamp,dispatch_ms:t-e.timeStamp,next_frame_ms:performance.now()-e.timeStamp}))}
</script><script defer src="/critical.js"></script></head><body><button onclick="clickMe(event)">Click count: <span id="clicks">0</span></button><div id="motion">●</div><h1>Local media-heavy page</h1><p>Scroll and click while the checked images arrive.</p><section>IMAGES</section>VIDEOS</body></html>'''


class Origin(BaseHTTPRequestHandler):
    def do_GET(self):
        path = urlsplit(self.path).path
        if path == '/':
            # Six image origins let the browser issue a burst instead of
            # serialising every request behind HTTP/1's six-connection limit.
            intrinsic = self.server.shared.scenario == 'intrinsic'
            dimensions = '' if intrinsic else 'width="240" height="160"'
            images = ''.join(f'<img crossorigin="anonymous" src="http://127.0.0.{2+i%6}:{self.server.server_port}/image/{i%24}.jpg?copy={i//24}" {dimensions}>' for i in range(6 if intrinsic else 36))
            videos = ''.join(f'<video muted preload="auto" width="240" src="http://127.0.0.{8+i}:{self.server.server_port}/clip/{i}.mp4"></video>' for i in range(2)) if self.server.shared.scenario == 'inference' else ''
            body = PAGE.replace('IMAGES', images).replace('VIDEOS', videos)
            if intrinsic:
                body = body.replace('<section>', '<section class="intrinsic">')
            body = body.encode()
            kind = 'text/html'
        elif path.startswith('/image/'):
            body = (self.server.shared.assets / Path(path).name).read_bytes()
            kind = 'image/jpeg'
        elif path.startswith('/clip/'):
            body = (self.server.shared.assets / 'clip.mp4').read_bytes()
            kind = 'video/mp4'
        elif path == '/critical.js':
            body, kind = b'window.criticalReady=performance.now()', 'application/javascript'
        else:
            body, kind = b'ok', 'text/plain'
        self.send_response(200)
        self.send_header('Content-Type', kind)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Timing-Allow-Origin', '*')
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, *_):
        pass


def validate(raw, proxy, scenario, placeholder_colors=None):
    """Correctness gates, deliberately independent of speed thresholds."""
    errors = []
    expected_count = 6 if scenario == 'intrinsic' else 36
    if len(raw['images']) != expected_count:
        errors.append(f"expected {expected_count} image elements")
    media = {entry['path']: entry for entry in proxy['responses']}
    browser_responses = {entry['url']: entry for entry in raw['http_responses']}
    geometry_ok = hidden_ok = covers_ok = 0
    for item in raw['images']:
        url = urlsplit(item['src'])
        key = url.path + ('?' + url.query if url.query else '')
        index = int(Path(url.path).stem)
        expected = (1600 + index * 4, 1000 + index * 3)
        response = media.get(key)
        if response is None:
            errors.append(f"filter response path was not observed: {key}")
            continue
        if not item['complete'] or (item['width'], item['height']) != expected:
            errors.append(f"wrong image geometry for {key}: {item['width']}x{item['height']}, expected {expected}")
        else:
            geometry_ok += 1
        if scenario == 'intrinsic':
            expected_height = 240 * expected[1] / expected[0]
            if abs(item['render_width'] - 240) > 1 or abs(item['render_height'] - expected_height) > 1:
                errors.append(f"intrinsic image layout collapsed or changed aspect ratio: {key}")
        if response['status'] != 200:
            errors.append(f"unexpected image status {response['status']}: {key}")
        actual_header = browser_responses.get(item['src'], {}).get('filter_result', 'passed')
        if actual_header != response['result']:
            errors.append(f"browser/filter result mismatch: {key}")
        pixels = item.get('pixels', [])
        if response['result'] == 'image-hidden':
            # Verify decoded output, not just a success status or a header.
            # Brave may perturb canvas RGB by one value for fingerprinting
            # protection. Allow two levels, but require complete opacity.
            expected_color = (placeholder_colors or {}).get(str(index), [214, 214, 214])
            if len(pixels) == 9 * 9 * 4 and all(
                    all(abs(channel - expected) <= 2
                        for channel, expected in zip(pixels[i:i+3], expected_color))
                    and pixels[i+3] == 255 for i in range(0, len(pixels), 4)):
                hidden_ok += 1
            else:
                errors.append(f"hidden response was not the expected opaque solid tile {expected_color}: {key}")
        elif response['result'].startswith('image-covered='):
            # The fixture seeds this exact centre region. Random source
            # noise is high variance; an actual frosted patch is smooth.
            channels = [pixels[channel::4] for channel in range(3)]
            variance = [sum((v - sum(c)/len(c))**2 for v in c)/len(c)
                        for c in channels if c]
            if len(pixels) == 9 * 9 * 4 and variance and max(variance) < 900:
                covers_ok += 1
            else:
                errors.append(f"cover region was not visibly transformed: {key}")
        elif scenario in ('hidden', 'intrinsic', 'covers'):
            errors.append(f"image was passed without the requested transform: {key}")
        if item.get('pixel_error'):
            errors.append(f"cannot inspect decoded image {key}: {item['pixel_error']}")
    if scenario in ('hidden', 'intrinsic') and hidden_ok != expected_count:
        errors.append(f"verified {hidden_ok}/{expected_count} hidden tiles")
    if scenario == 'covers' and covers_ok == 0:
        errors.append('no successfully rendered cover reached the browser')
    if scenario == 'inference':
        if proxy['inferences'] <= 0:
            errors.append('real detector did not execute')
        if len(raw['videos']) != 2:
            errors.append('expected two video elements')
        for video in raw['videos']:
            response = media.get(urlsplit(video['src']).path)
            if response is None or not response['result'].startswith('video-'):
                errors.append(f"video filtering path was not observed: {video['src']}")
            elif response['status'] == 403:
                continue  # An explicit filtering refusal is reported, not a codec error.
            elif video['error'] or video['readyState'] < 1:
                errors.append(f"passed video did not decode in the browser: {video}")
    if proxy.get('image_status_supported'):
        ui = raw['image_status_ui']
        if ui['hosts'] != 1:
            errors.append('production image-status UI did not initialize')
        if 200 not in proxy['status_api_http_codes']:
            errors.append('production image-status API did not succeed')
        # Offscreen unfinished images stop polling. Include hidden approvals
        # so an all-approved viewport still proves completed status handling.
        active = [badge for badge in ui['badges'] if badge['visible'] or
                  (badge['was_displayed'] and badge['state'] in ('checked', 'not_checked'))]
        if not active or any(badge['state'] in ('waiting', 'checking', 'unknown') for badge in active):
            errors.append('observed production image-status states did not settle')
        if any(badge['visible'] and badge['state'] in ('checked', 'not_checked') for badge in ui['badges']):
            errors.append('approved images still display a final status badge')
    return {'valid': not errors, 'errors': errors, 'geometry_preserved': geometry_ok,
            'verified_hidden_tiles': hidden_ok, 'verified_covers': covers_ok}


def source_metadata(root):
    paths = ('mitm/kosher_filter.py', 'mitm/kosher_page.js', 'kosherd/src/kosherd/vision.py',
             'kosherd/src/kosherd/videocheck.py', 'kosherd/src/kosherd/imageedit.py')
    commit_result = subprocess.run(['git', '-C', str(root), 'rev-parse', 'HEAD'],
                                  capture_output=True, text=True)
    commit = commit_result.stdout.strip() if commit_result.returncode == 0 else None
    diff = subprocess.run(['git', '-C', str(root), 'diff', 'HEAD', '--', *paths],
                          capture_output=True).stdout if commit else None
    return {'root': str(root.resolve()), 'commit': commit,
            'tracked_diff_sha256': hashlib.sha256(diff).hexdigest() if diff is not None else None,
            'files_sha256': {path: hashlib.sha256((root/path).read_bytes()).hexdigest()
                             for path in paths}}


def host_metadata():
    def read(path):
        try:
            return Path(path).read_text().strip()
        except OSError:
            return None

    cpu_info = read('/proc/cpuinfo') or ''
    return {'architecture': platform.machine(), 'kernel': platform.release(),
            'logical_cpus': os.cpu_count(), 'python': platform.python_version(),
            'cpu_model': next((line.split(':', 1)[1].strip() for line in cpu_info.splitlines()
                               if line.startswith('model name')), None),
            'platform_profile': read('/sys/firmware/acpi/platform_profile'),
            'energy_preferences': sorted({value for path in Path('/sys/devices/system/cpu').glob(
                'cpu*/cpufreq/energy_performance_preference') if (value := read(path))}),
            'governors': sorted({value for path in Path('/sys/devices/system/cpu').glob(
                'cpu*/cpufreq/scaling_governor') if (value := read(path))}),
            'load_average': os.getloadavg()}


def power_settings(host):
    return {key: host[key] for key in
            ('platform_profile', 'energy_preferences', 'governors')}


def expected_placeholder_colors(root, asset_dir, extra_paths):
    """Compute expected decoded colors in a separate process BEFORE trials.

    Baseline tiles are neutral; the candidate deliberately samples the
    source's average color. This must not warm the measured proxy caches.
    Pixel opacity, constant-color output and geometry remain independent
    browser assertions, including detecting unsafe fallback shortcuts.
    """
    code = '''import io,json,sys
from pathlib import Path
from PIL import Image
from kosherd import imageedit
colors={}
for path in Path(sys.argv[1]).glob('*.jpg'):
    tile=imageedit.placeholder_for(path.read_bytes())
    with Image.open(io.BytesIO(tile)) as image:
        pixel=image.convert('RGBA').getpixel((0,0))
    assert pixel[3]==255, 'expected placeholder must be opaque'
    colors[path.stem]=pixel[:3]
print(json.dumps(colors))
'''
    env=dict(os.environ, PYTHONPATH=os.pathsep.join(
        [str(root/'kosherd/src')]+[str(path) for path in extra_paths]))
    result=subprocess.run(['/usr/bin/python3','-c',code,str(asset_dir)],
                          env=env,capture_output=True,text=True,check=True)
    return json.loads(result.stdout)


async def trial(browser, origin, args, label, root, scenario, repeat):
    out = args.output / f'{label}-{scenario}-{repeat}'
    out.mkdir(parents=True, exist_ok=False)
    extra_paths = args.candidate_pythonpath if label == 'after' else args.baseline_pythonpath
    runtime_paths = [str(root/'kosherd/src')]+[str(path) for path in extra_paths]
    host_start = host_metadata()
    power_start = power_settings(host_start)
    if args.expected_power_settings is None:
        args.expected_power_settings = power_start
    environment = {'source': source_metadata(root), 'host': host_start,
                   'runtime_pythonpath': runtime_paths,
                   'expected_power_settings': args.expected_power_settings}
    (out/'environment.json').write_text(json.dumps(environment, indent=2))
    env = dict(os.environ, PYTHONPATH=os.pathsep.join(runtime_paths), MEDIA_PERF_ROOT=str(root),
               MEDIA_PERF_OUT=str(out), MEDIA_PERF_ASSETS=str(origin.assets), MEDIA_PERF_SCENARIO=scenario)
    log = (out / 'proxy.log').open('w')
    process = subprocess.Popen([args.mitmdump, '--listen-host', '127.0.0.1', '--listen-port', '8087',
        '--set', f'confdir={out / "mitm-conf"}', '--set', 'flow_detail=0', '--set', 'termlog_verbosity=warn',
        '--set', 'connection_strategy=lazy', '-s', str(HERE / 'addon.py')], env=env, stdout=log, stderr=subprocess.STDOUT)
    page = None
    try:
        for _ in range(1200):
            if (out / 'ready').exists():
                break
            if process.poll() is not None:
                raise RuntimeError(f'Proxy exited; see {out / "proxy.log"}')
            await asyncio.sleep(.1)
        else:
            raise TimeoutError('Proxy model warmup exceeded 120 seconds')
        origin.scenario = scenario
        context = browser.contexts[0]
        page = await context.new_page()
        await page.bring_to_front()
        cdp = await context.new_cdp_session(page)
        await cdp.send('Network.enable')
        await cdp.send('Network.setCacheDisabled', {'cacheDisabled': True})
        await cdp.send('Emulation.setDeviceMetricsOverride', {'width':1280, 'height':800, 'deviceScaleFactor':1, 'mobile':False})
        responses = []
        page.on('response', lambda response: responses.append({
            'url': response.url, 'status': response.status,
            'filter_result': response.headers.get('x-kosheros', 'passed')}))
        await page.goto(f'http://127.0.0.1:{origin.server_port}/', wait_until='commit', timeout=60000)
        start = time.monotonic()
        # Real CDP events while media loads. EventTiming reports the browser's
        # interaction duration; event-to-rAF is only a next-frame proxy, not
        # actual presentation time or a full page-lifetime INP measurement.
        dispatches = []
        while time.monotonic()-start < args.seconds:
            sent = time.monotonic()
            await cdp.send('Input.dispatchMouseEvent', {'type':'mousePressed','x':60,'y':32,'button':'left','clickCount':1})
            await cdp.send('Input.dispatchMouseEvent', {'type':'mouseReleased','x':60,'y':32,'button':'left','clickCount':1})
            dispatches.append({'time_ms':(sent-start)*1000,'roundtrip_ms':(time.monotonic()-sent)*1000})
            await cdp.send('Input.dispatchMouseEvent', {'type':'mouseWheel','x':800,'y':600,'deltaX':0,'deltaY':120 if int((time.monotonic()-start)/2)%2==0 else -120})
            await asyncio.sleep(.15)
        proxy = await page.evaluate("fetch('/__metrics').then(r=>r.json())")
        raw = await page.evaluate('''() => ({...metrics,criticalReady:window.criticalReady,navigation:performance.getEntriesByType('navigation')[0].toJSON(),resources:performance.getEntriesByType('resource').map(r=>({name:r.name,startTime:r.startTime,duration:r.duration})),clicks:document.getElementById('clicks').textContent})''')
        # Pixel inspection runs AFTER the timing snapshot: canvas decoding
        # and readback must not contaminate the measured interaction window.
        raw.update(await page.evaluate('''() => ({images:[...document.images].map(i=>{
            const rect=i.getBoundingClientRect();
            const result={src:i.currentSrc||i.src,complete:i.complete,width:i.naturalWidth,height:i.naturalHeight,render_width:rect.width,render_height:rect.height};
            try{const c=document.createElement('canvas');c.width=c.height=9;const x=c.getContext('2d');x.drawImage(i,301,246,9,9,0,0,9,9);result.pixels=[...x.getImageData(0,0,9,9).data]}catch(e){result.pixel_error=String(e)}
            return result}),videos:[...document.querySelectorAll('video')].map(v=>({src:v.currentSrc||v.src,readyState:v.readyState,networkState:v.networkState,width:v.videoWidth,height:v.videoHeight,error:v.error?{code:v.error.code,message:v.error.message}:null}))})'''))
        raw['image_status_ui'] = await page.evaluate(IMAGE_STATUS_SNAPSHOT)
        raw['dispatches'] = dispatches
        raw['http_responses'] = responses
        # Timing and proxy snapshots are already frozen. These provenance
        # reads must not add work inside the measured interaction window.
        host_end = host_metadata()
        power_end = power_settings(host_end)
        environment['host_end'] = host_end
        (out/'environment.json').write_text(json.dumps(environment, indent=2))
        frames = [entry['gap_ms'] for entry in raw['frames']]
        inputs = [entry['next_frame_ms'] for entry in raw['inputs']]
        # Events below the browser's minimum reporting threshold are absent.
        # Keep counts and label this as a slow-event tail, not all-input p95.
        interactions = {}
        for event in raw['events']:
            if event['interactionId']:
                interactions[event['interactionId']] = max(
                    interactions.get(event['interactionId'], 0), event['duration'])
        load_end = raw['navigation']['loadEventEnd']
        loading_frames = [entry['gap_ms'] for entry in raw['frames']
                          if not load_end or entry['time'] <= load_end]
        validation = validate(raw, proxy, scenario, args.placeholder_colors[label])
        power_errors = []
        if power_start != power_end:
            power_errors.append('power settings changed during this trial')
        if power_start != args.expected_power_settings or power_end != args.expected_power_settings:
            power_errors.append('power settings differ from the first trial; mixed-mode comparisons are invalid')
        validation['errors'].extend(power_errors)
        validation['valid'] = not validation['errors']
        result = {'version':label,'scenario':scenario,'repeat':repeat,'browser':browser.version,
            'critical_js_ms':round(raw.get('criticalReady') or 0,2),
            'dom_content_loaded_ms':round(raw['navigation']['domContentLoadedEventEnd'],2),
            'load_ms':round(load_end,2) if load_end else None,
            'frame_gap_p95_ms':percentile(frames,.95),'frame_gap_max_ms':percentile(frames,1),
            'frames_over_50ms':sum(x>50 for x in frames),
            'loading_frame_gap_p95_ms':percentile(loading_frames,.95),
            'loading_frame_gap_max_ms':percentile(loading_frames,1),
            'input_next_frame_p95_ms':percentile(inputs,.95),'input_next_frame_max_ms':percentile(inputs,1),
            'reported_interactions':len(interactions),
            'reported_interaction_max_ms':max(interactions.values(),default=None),
            'input_dispatches':len(dispatches),
            'cdp_dispatch_roundtrip_max_ms':percentile([entry['roundtrip_ms'] for entry in dispatches],1),
            'clicks':int(raw['clicks']),'long_tasks':len(raw['longtasks']),
            'long_task_total_ms':round(sum(entry['duration'] for entry in raw['longtasks']),2),
            'layout_shift_sum':round(sum(entry['value'] for entry in raw['shifts'] if not entry['hadRecentInput']),6),
            'layout_shift_sum_including_input':round(sum(entry['value'] for entry in raw['shifts']),6),
            'ping_p95_ms':percentile(raw['pings'],.95),'ping_max_ms':percentile(raw['pings'],1),
            'proxy_lag_p95_ms':percentile(proxy['loop_lag_ms'],.95),'proxy_lag_max_ms':percentile(proxy['loop_lag_ms'],1),
            'inferences':proxy['inferences'],'image_complete':sum(i['complete'] and i['width']>0 for i in raw['images']),
            'media_results':dict(Counter(r['result'] for r in proxy['responses'])),
            'image_status_ui_hosts':raw['image_status_ui']['hosts'],
            'visible_image_status_labels':dict(Counter(b['label'] for b in raw['image_status_ui']['badges'] if b['visible'])),
            'observed_image_status_states':dict(Counter(b['state'] for b in raw['image_status_ui']['badges'] if b['was_displayed'])),
            'status_api_http_codes':dict(Counter(proxy['status_api_http_codes'])),
            'video_states':raw['videos'], 'validation':validation,
            'power':{'start':power_start, 'end':power_end,
                     'expected':args.expected_power_settings, 'valid':not power_errors},
            'capture_status':'pending', 'capture_errors':[]}
        # A screenshot failure must not discard measurements or correctness
        # evidence. Persist everything before asking Chromium for the PNG.
        (out/'browser.json').write_text(json.dumps(raw,indent=2))
        (out/'proxy.json').write_text(json.dumps(proxy,indent=2))
        (out/'summary.json').write_text(json.dumps(result,indent=2))
        capture_error = await capture_screenshot(cdp, out/'browser.png')
        if capture_error:
            result['capture_errors'].append(capture_error)
        result['capture_status'] = 'failed' if capture_error else 'saved'
        (out/'summary.json').write_text(json.dumps(result,indent=2))
        print(json.dumps(result), flush=True)
        return result
    finally:
        if page:
            await page.close()
        process.send_signal(signal.SIGINT)
        try:
            await asyncio.to_thread(process.wait, timeout=30)
        except subprocess.TimeoutExpired:
            process.kill()
            await asyncio.to_thread(process.wait)
        log.close()


async def main(args):
    args.output.mkdir(parents=True, exist_ok=True)
    args.expected_power_settings = None
    assets(args.output/'assets')
    args.placeholder_colors={}
    for label,root,extra in [('before',args.baseline,args.baseline_pythonpath),
                             ('after',args.candidate,args.candidate_pythonpath)]:
        colors=expected_placeholder_colors(root,args.output/'assets',extra)
        args.placeholder_colors[label]=colors
        (args.output/f'expected-placeholders-{label}.json').write_text(json.dumps(colors,indent=2))
    origin = ThreadingHTTPServer(('127.0.0.1',0), Origin)
    origin.assets, origin.scenario = args.output/'assets', 'hidden'
    servers = [origin] + [ThreadingHTTPServer((f'127.0.0.{i}', origin.server_port), Origin)
                          for i in range(2, 10)]
    for server in servers:
        server.shared = origin
        threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .05}, daemon=True).start()
    results=[]
    try:
        async with async_playwright() as pw:
            browser=await pw.chromium.connect_over_cdp(args.cdp)
            session = await browser.new_browser_cdp_session()
            version = await session.send('Browser.getVersion')
            try:
                command_line = await session.send('Browser.getBrowserCommandLine')
            except Exception:
                # Chromium exposes this command only with automation enabled.
                # Read only the explicitly selected local debugging instance.
                port = urlsplit(args.cdp).port
                command_line = {'arguments': None}
                for path in Path('/proc').glob('[0-9]*/cmdline'):
                    try:
                        argv = path.read_bytes().decode().rstrip('\0').split('\0')
                    except (OSError, UnicodeDecodeError):
                        continue
                    if argv and Path(argv[0]).name in ('brave', 'chromium', 'chrome') \
                            and f'--remote-debugging-port={port}' in argv \
                            and not any(arg.startswith('--type=') for arg in argv):
                        command_line = {'arguments': argv}
                        break
            environment = {
                'browser': version, 'browser_command_line': command_line,
                'host': host_metadata(), 'seconds': args.seconds, 'repeats': args.repeats,
                'scenarios': args.scenarios,
                'harness_sha256': {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                                   for path in (HERE/'browser.py', HERE/'addon.py')},
                'assets_sha256': {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                                  for path in (args.output/'assets').iterdir()}}
            (args.output/'environment.json').write_text(json.dumps(environment, indent=2))
            for repeat in range(args.repeats):
                variants=[('before',args.baseline),('after',args.candidate)]
                if repeat%2:
                    variants.reverse()
                for scenario in args.scenarios:
                    for label,root in variants:
                        results.append(await trial(browser,origin,args,label,root,scenario,repeat))
                        (args.output/'summary.json').write_text(json.dumps(results,indent=2))
                        environment['expected_power_settings'] = args.expected_power_settings
                        (args.output/'environment.json').write_text(json.dumps(environment,indent=2))
                        if not results[-1]['power']['valid']:
                            raise SystemExit('Power settings changed; artifacts retained, but this run cannot be compared. Restart in one stable mode.')
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()
    invalid = [r for r in results if not r['validation']['valid'] or r['capture_errors']]
    if invalid:
        raise SystemExit(f"{len(invalid)} trials failed correctness or screenshot checks; inspect summary.json")


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline',type=Path,required=True)
    parser.add_argument('--candidate',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--cdp',default='http://127.0.0.1:9223')
    parser.add_argument('--mitmdump',default='/usr/bin/mitmdump')
    parser.add_argument('--candidate-pythonpath',type=Path,action='append',default=[])
    parser.add_argument('--baseline-pythonpath',type=Path,action='append',default=[])
    parser.add_argument('--seconds',type=float,default=15)
    parser.add_argument('--repeats',type=int,default=3)
    parser.add_argument('--scenarios',nargs='+',choices=['hidden','intrinsic','covers','inference'],default=['hidden','intrinsic','covers','inference'])
    asyncio.run(main(parser.parse_args()))
