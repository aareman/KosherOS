#!/usr/bin/env python3
"""Public Amazon/Prime functional evidence in a dedicated headed Brave profile.

This is a live-site coverage check, not a controlled performance A/B. Uses only
an isolated profile and temporary CA key pin. Upstream TLS verification stays
on. Never logs into an account or submits a search/purchase/subscription.
"""
import argparse
import asyncio
from collections import Counter
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import time
from urllib.request import urlopen

from playwright.async_api import async_playwright

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('media_bench', HERE/'browser.py')
bench = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bench)

METRICS = r'''(() => {
window.__mediaEvidence={frames:[],events:[],longtasks:[],shifts:[],paints:[],errors:[],imageStatuses:[]};
const m=window.__mediaEvidence;let last=performance.now();
function frame(t){m.frames.push({time:t,gap:t-last});last=t;requestAnimationFrame(frame)}requestAnimationFrame(frame);
for(const type of ['event','longtask','layout-shift','paint','largest-contentful-paint'])try{new PerformanceObserver(list=>{for(const e of list.getEntries()){const value={name:e.name,time:e.startTime,duration:e.duration};if(type==='event'){value.interactionId=e.interactionId;m.events.push(value)}else if(type==='longtask')m.longtasks.push(value);else if(type==='layout-shift')m.shifts.push({time:e.startTime,value:e.value,hadRecentInput:e.hadRecentInput});else m.paints.push(value)}}).observe({type,buffered:true,durationThreshold:16})}catch(e){}
window.addEventListener('error',e=>m.errors.push(String(e.message)));
// Observe the production UI's open shadow root without changing its timing.
const watched=new WeakSet();let previous='';
function statuses(){const badges=[...document.querySelectorAll('[data-kosheros-ui="images"]')].flatMap(host=>[...(host.shadowRoot?.querySelectorAll('.badge')||[])].map(b=>({state:b.getAttribute('data-state'),label:b.textContent,visible:!b.hidden})));const key=JSON.stringify(badges);if(key!==previous){m.imageStatuses.push({time:performance.now(),badges});previous=key}}
function watch(){for(const host of document.querySelectorAll('[data-kosheros-ui="images"]')){if(host.shadowRoot&&!watched.has(host)){watched.add(host);new MutationObserver(statuses).observe(host.shadowRoot,{subtree:true,childList:true,attributes:true,attributeFilter:['data-state','hidden']});statuses()}}}
new MutationObserver(watch).observe(document,{subtree:true,childList:true});
})()'''


def ca_fingerprint(directory):
    code = """from mitmproxy.certs import CertStore
from cryptography import x509
from cryptography.hazmat.primitives import serialization
import base64,hashlib,sys
from pathlib import Path
p=Path(sys.argv[1]);p.mkdir(parents=True,exist_ok=True)
CertStore.from_store(p,'mitmproxy',2048)
c=x509.load_pem_x509_certificate((p/'mitmproxy-ca-cert.pem').read_bytes())
print(base64.b64encode(hashlib.sha256(c.public_key().public_bytes(serialization.Encoding.DER,serialization.PublicFormat.SubjectPublicKeyInfo)).digest()).decode())
"""
    return subprocess.run(['/usr/bin/python3','-c',code,str(directory)],
                          check=True,capture_output=True,text=True).stdout.strip()


async def read_stats():
    def fetch():
        # Direct HTTP request through only our local proxy; not page-origin
        # JS (which Amazon's CSP would prevent from reaching loopback).
        import http.client
        connection=http.client.HTTPConnection('127.0.0.1',8088,timeout=30)
        connection.request('GET','http://127.0.0.1/__amazon_metrics')
        response=connection.getresponse()
        data=json.loads(response.read())
        connection.close()
        return data
    return await asyncio.to_thread(fetch)


async def capture(browser, url, output, seconds, require_image_status=False):
    page=await browser.contexts[0].new_page()
    await page.bring_to_front()
    await page.add_init_script(METRICS)
    responses=[]
    page.on('response',lambda response: responses.append({
        'url':response.url,'status':response.status,
        'mime':response.headers.get('content-type',''),
        'filter_result':response.headers.get('x-kosheros')}))
    session=await page.context.new_cdp_session(page)
    await session.send('Network.enable')
    await session.send('Network.setCacheDisabled',{'cacheDisabled':True})
    await session.send('Emulation.setDeviceMetricsOverride',{
        'width':1440,'height':900,'deviceScaleFactor':1,'mobile':False})
    await page.goto(url,wait_until='domcontentloaded',timeout=90000)
    capture_errors=[]
    initial_error=await bench.capture_screenshot(session,output/'initial.png')
    if initial_error:
        capture_errors.append(initial_error)
    actions=[]
    # Focus and type into the ordinary search box without submitting anything.
    search=page.locator('#twotabsearchtextbox,input[name="field-keywords"],input[type="search"],input[name="search_query"]').first
    if 'amazon.com' in url or 'youtube.com' in url:
        try:
            await search.wait_for(state='visible',timeout=10000)
        except Exception:
            pass
    if await search.count() and await search.is_visible():
        await search.click()
        await search.fill('books')
        await search.fill('')
        await search.press('Escape')
        actions.append('focused search, typed books, cleared, escaped; no submission')
    start=time.monotonic()
    while time.monotonic()-start < seconds:
        await session.send('Input.dispatchMouseEvent',{
            'type':'mouseWheel','x':1150,'y':750,'deltaX':0,
            'deltaY':350 if int((time.monotonic()-start)/6)%2==0 else -350})
        await asyncio.sleep(.75)
    await page.evaluate('scrollTo(0,0)')
    await asyncio.sleep(2)
    controls=await page.locator('button,[role=button],a[aria-label]').evaluate_all('''els=>els.map(e=>({tag:e.tagName,label:e.getAttribute('aria-label')||e.textContent.trim().slice(0,80),visible:!!(e.offsetWidth||e.offsetHeight)})).filter(e=>e.visible&&/next|previous|carousel/i.test(e.label)).slice(0,30)''')
    # Only exact next-slide labels; never generic navigation or commerce.
    next_button=page.locator('a[aria-label="Carousel next slide"],button[aria-label="Next slide"],button[aria-label="Next Slide"]').first
    if await next_button.count() and await next_button.is_visible():
        await next_button.click()
        actions.append('clicked next carousel button')
        await asyncio.sleep(3)
    raw=await page.evaluate('''() => ({metrics:window.__mediaEvidence,title:document.title,url:location.href,navigation:performance.getEntriesByType('navigation')[0]?.toJSON(),images:[...document.images].map(i=>{let r=i.getBoundingClientRect();return {src:i.src,currentSrc:i.currentSrc,alt:i.alt,srcset:i.srcset,complete:i.complete,naturalWidth:i.naturalWidth,naturalHeight:i.naturalHeight,width:r.width,height:r.height,visible:r.width>0&&r.height>0&&r.bottom>0&&r.top<innerHeight}}),backgrounds:[...document.querySelectorAll('*')].map(e=>{const image=getComputedStyle(e).backgroundImage;const r=e.getBoundingClientRect();return {image,width:r.width,height:r.height,visible:r.width>0&&r.height>0&&r.bottom>0&&r.top<innerHeight}}).filter(e=>e.image!=='none'),bodyText:document.body.innerText.slice(0,2500)})''')
    raw['image_status_ui']=await page.evaluate(bench.IMAGE_STATUS_SNAPSHOT)
    raw['http_responses']=responses
    raw['actions']=actions
    raw['carousel_controls']=controls
    (output/'browser.json').write_text(json.dumps(raw,indent=2))
    stats=await read_stats()
    (output/'proxy-snapshot.json').write_text(json.dumps(stats,indent=2))
    frames=[entry['gap'] for entry in raw['metrics']['frames']]
    interactions={}
    for entry in raw['metrics']['events']:
        if entry.get('interactionId'):
            interactions[entry['interactionId']]=max(interactions.get(entry['interactionId'],0),entry['duration'])
    summary={'url':raw['url'],'title':raw['title'],'images':len(raw['images']),
        'visible_images':sum(i['visible'] for i in raw['images']),
        'visible_decoded_images':sum(i['visible'] and i['complete'] and i['naturalWidth']>0 for i in raw['images']),
        'visible_css_backgrounds':sum(b['visible'] for b in raw['backgrounds']),
        'frame_gap_p95_ms':bench.percentile(frames,.95),'frame_gap_max_ms':bench.percentile(frames,1),
        'reported_interaction_max_ms':max(interactions.values(),default=None),
        'media_results':dict(Counter(r['result'] for r in stats['responses'])),
        'media_results_scope':'cumulative across pages in this isolated proxy run',
        'input_mimes':dict(Counter(r['input_mime'] for r in stats['responses'])),
        'upstream_already_filtered':sum(bool(r['upstream_filter_result']) for r in stats['responses']),
        'proxy_errors':len(stats['errors']),'actions':actions,
        'image_status_ui_hosts':raw['image_status_ui']['hosts'],
        'visible_image_status_labels':dict(Counter(b['label'] for b in raw['image_status_ui']['badges'] if b['visible'])),
        'final_tracked_image_status_states':dict(Counter(b['state'] for b in raw['image_status_ui']['badges'] if b['was_displayed'])),
        'observed_image_status_states':sorted({b['state'] for event in raw['metrics']['imageStatuses'] for b in event['badges'] if b['state']}),
        'status_api_http_codes':dict(Counter(r['status'] for r in stats['status_api_responses'])),
        'has_captcha_text':any(text in raw['bodyText'].lower() for text in ('enter the characters','robot check','verify that you'))}
    failures=[]
    if require_image_status and url.startswith('http://127.0.0.1/__'):
        # Local fixture HTML has no upstream host-script token. It therefore
        # proves the complete production injection/auth/status/UI path.
        if raw['image_status_ui']['hosts'] != 1:
            failures.append('expected exactly one production image-status UI')
        if not any(r['status']==200 for r in stats['status_api_responses']):
            failures.append('production image-status API never succeeded')
        active=[b for b in raw['image_status_ui']['badges'] if b['visible'] or
                (b['was_displayed'] and b['state'] in ('checked','not_checked'))]
        if not active or any(b['state'] in ('waiting','checking','unknown') for b in active):
            failures.append('observed image-status states did not settle')
        if any(b['visible'] and b['state'] in ('checked','not_checked') for b in raw['image_status_ui']['badges']):
            failures.append('approved images still display a final status badge')
        if 'waiting' not in summary['observed_image_status_states']:
            failures.append('initial waiting state was not observed')
    summary['validation_errors']=failures
    summary['capture_status']='pending'
    summary['capture_errors']=capture_errors
    # Preserve raw data, proxy data and validation before the final screenshot.
    (output/'summary.json').write_text(json.dumps(summary,indent=2))
    final_error=await bench.capture_screenshot(session,output/'final.png')
    if final_error:
        capture_errors.append(final_error)
    summary['capture_status']='failed' if capture_errors else 'saved'
    (output/'summary.json').write_text(json.dumps(summary,indent=2))
    print(json.dumps(summary),flush=True)
    await page.close()
    return failures+[f"Screenshot capture failed: {error}" for error in capture_errors]


async def main(args):
    args.output.mkdir(parents=True,exist_ok=False)
    shared=Path('/tmp/kosher-amazon-evidence')
    shared.mkdir(exist_ok=True)
    ca=shared/'ca'
    pin=ca_fingerprint(ca)
    # Failed captures can leave restorable tabs carrying the previous proxy's
    # page token. Every run needs a genuinely fresh isolated browser profile.
    profile=Path(tempfile.mkdtemp(prefix=f'browser-profile-{args.output.name}-',dir=shared))
    browser_args=['flatpak','run','com.brave.Browser',f'--user-data-dir={profile}',
        '--remote-debugging-port=9224','--no-first-run','--no-default-browser-check',
        '--disable-background-networking','--proxy-server=http://127.0.0.1:8088',
        '--proxy-bypass-list=<-loopback>',f'--ignore-certificate-errors-spki-list={pin}','about:blank']
    paths=[str(args.root/'kosherd/src')]+[str(path) for path in args.extra_pythonpath]
    env=dict(os.environ,PYTHONPATH=os.pathsep.join(paths),
        AMAZON_PERF_ROOT=str(args.root),AMAZON_PERF_OUT=str(args.output),AMAZON_PERF_LEVEL=args.level,AMAZON_PERF_ASSET_LIST=str(args.asset_list or ''))
    proxylog=(args.output/'proxy.log').open('w')
    proxy=subprocess.Popen(['/usr/bin/mitmdump','--listen-host','127.0.0.1','--listen-port','8088',
        '--set',f'confdir={ca}','--set','ssl_insecure=false',
        '--set','ssl_verify_upstream_trusted_ca=/etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem',
        '--set','flow_detail=0','--set','termlog_verbosity=warn','--set','connection_strategy=lazy',
        '-s',str(HERE/'amazon_addon.py')],env=env,stdout=proxylog,stderr=subprocess.STDOUT)
    browserprocess=None
    browserlog=(args.output/'browser.log').open('w')
    try:
        for _ in range(1200):
            if (args.output/'ready').exists():break
            if proxy.poll() is not None:raise RuntimeError('proxy exited; inspect proxy.log')
            await asyncio.sleep(.1)
        else:raise TimeoutError('proxy model startup')
        browserprocess=subprocess.Popen(browser_args,stdout=browserlog,stderr=subprocess.STDOUT)
        for _ in range(300):
            try:
                version=await asyncio.to_thread(lambda:json.load(urlopen('http://127.0.0.1:9224/json/version',timeout=1)))
                break
            except Exception:await asyncio.sleep(.1)
        else:raise TimeoutError('isolated Brave CDP startup')
        (args.output/'environment.json').write_text(json.dumps({
            'source':bench.source_metadata(args.root),'host':bench.host_metadata(),
            'browser':version,'browser_args':browser_args,'temporary_ca_spki':pin,
            'harness_sha256':{path.name:hashlib.sha256(path.read_bytes()).hexdigest()
                              for path in (HERE/'browser.py',HERE/'amazon.py',HERE/'amazon_addon.py')},
            'upstream_tls_verification':True,'media_level':args.level,
            'extra_pythonpath':[str(path) for path in args.extra_pythonpath],
            'scope':'public signed-out site; isolated media policy; no real account configuration'},indent=2))
        async with async_playwright() as pw:
            browser=await pw.chromium.connect_over_cdp('http://127.0.0.1:9224')
            failures=[]
            try:
                await browser.contexts[0].clear_cookies()
                for index,url in enumerate(args.urls):
                    output=args.output/f'page-{index}'
                    output.mkdir()
                    failures.extend(await capture(browser,url,output,args.seconds,args.require_image_status))
            finally:
                # connect_over_cdp().close() only disconnects; explicitly close
                # the isolated browser, including when a capture fails.
                await (await browser.new_browser_cdp_session()).send('Browser.close')
            if failures:
                raise AssertionError('; '.join(failures))
    finally:
        proxy.send_signal(signal.SIGINT)
        try:await asyncio.to_thread(proxy.wait,timeout=30)
        except subprocess.TimeoutExpired:proxy.kill();await asyncio.to_thread(proxy.wait)
        if browserprocess and browserprocess.poll() is None:
            browserprocess.terminate()
        proxylog.close();browserlog.close()


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--level',choices=['nsfw','suggestive','immodest','people','all'],default='immodest')
    parser.add_argument('--seconds',type=float,default=20)
    parser.add_argument('--extra-pythonpath',type=Path,action='append',default=[])
    parser.add_argument('--asset-list',type=Path)
    parser.add_argument('--require-image-status',action='store_true',
                        help='Require working production status UI on the local asset check pages')
    parser.add_argument('--urls',nargs='+',default=['https://www.amazon.com/','https://www.primevideo.com/'])
    asyncio.run(main(parser.parse_args()))
