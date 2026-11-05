#!/usr/bin/env python3
"""Real-browser regression for image badges; intercepts only a .test fixture."""
import argparse
import asyncio
import io
import json
from pathlib import Path

from PIL import Image
from playwright.async_api import async_playwright


async def main(args):
    args.output.mkdir(parents=True, exist_ok=True)
    script = (Path(__file__).resolve().parents[2] / 'mitm/kosher_page.js').read_text()
    buffer = io.BytesIO()
    Image.new('RGB', (320, 180), (120, 150, 175)).save(buffer, 'PNG')
    image = buffer.getvalue()
    states = {'http://media-ui.test/first.png': 'checking'}
    urls = []
    html = '''<html><head><script defer src="/__kosheros__/filter.js" data-kosheros-language="off" data-kosheros-images="1" data-kosheros-token="fixture"></script></head><body><h1>Image status browser check</h1><button id="link" onclick="window.clicked=true" style="border:0;padding:0"><img id="photo" width="320" height="180" src="/first.png"></button></body></html>'''
    async with async_playwright() as pw:
        browser = await pw.chromium.connect_over_cdp(args.cdp)
        page = await browser.contexts[0].new_page()
        await page.bring_to_front()
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        try:
            async def route(request):
                url = request.request.url
                urls.append(url)
                if url.endswith('/filter.js'):
                    await request.fulfill(content_type='application/javascript; charset=utf-8', body=script)
                elif url.endswith('/image-status'):
                    assert request.request.headers['x-kosheros-page'] == 'fixture'
                    body = request.request.post_data
                    assert len(body) <= 20_000
                    requested = json.loads(body)['urls']
                    await request.fulfill(json={'images': {url: states[url] for url in requested if url in states}})
                elif '.png' in url:
                    await asyncio.sleep(.1)
                    await request.fulfill(content_type='image/png', body=image)
                else:
                    await request.fulfill(content_type='text/html; charset=utf-8', body=html)
            await page.route('http://media-ui.test/**', route)
            await page.goto('http://media-ui.test/', wait_until='load')
            badge = page.locator('[data-kosheros-ui="images"] .badge')
            await badge.filter(has_text='Checking…').wait_for()
            before = await page.locator('#photo').bounding_box()
            await page.screenshot(path=str(args.output / 'checking.png'))
            states['http://media-ui.test/first.png'] = 'filtered'
            await badge.filter(has_text='Filtered').wait_for()
            assert await page.locator('#photo').bounding_box() == before
            assert await page.locator('#photo').get_attribute('src') == '/first.png'
            await page.locator('#link').click()
            assert await page.evaluate('window.clicked')
            await page.screenshot(path=str(args.output / 'filtered.png'))
            states['http://media-ui.test/second.png'] = 'unavailable'
            await page.locator('#photo').evaluate('(img) => img.src="/second.png"')
            await badge.filter(has_text='Not checked · hidden').wait_for()
            assert await page.locator('#photo').bounding_box() == before
            await page.screenshot(path=str(args.output / 'unavailable.png'))
            states['http://media-ui.test/background.png'] = 'hidden'
            await page.evaluate('''() => {
                const tile = document.createElement('div'); tile.id = 'background';
                tile.style.cssText = 'width:320px;height:180px;background-image:url(/background.png)';
                document.body.appendChild(tile);
            }''')
            await badge.filter(has_text='Hidden').wait_for()
            await page.screenshot(path=str(args.output / 'background.png'))
            await page.locator('#background').evaluate('(tile) => tile.remove()')
            await page.locator('#photo').evaluate('(img) => img.remove()')
            await page.wait_for_function('!document.querySelector("[data-kosheros-ui]").shadowRoot.querySelector(".badge")')
            assert set(urls) <= {'http://media-ui.test/', 'http://media-ui.test/favicon.ico',
                                'http://media-ui.test/first.png', 'http://media-ui.test/second.png',
                                'http://media-ui.test/background.png',
                                'http://media-ui.test/__kosheros__/filter.js',
                                'http://media-ui.test/__kosheros__/image-status'}
            long_urls = ['http://media-ui.test/batch.png?id=' + str(i) + '&key=' + 'a' * 1700 for i in range(12)]
            states.update({url: 'checked' for url in long_urls})
            await page.evaluate('''urls => {
                const grid = document.createElement('div'); grid.id='batch';
                grid.style.cssText='display:grid;grid-template-columns:repeat(6,100px)';
                urls.forEach(url => {const img=new Image(100,60);img.src=url;grid.appendChild(img)});
                document.body.appendChild(grid);
            }''', long_urls)
            await page.wait_for_function('document.querySelector("[data-kosheros-ui]").shadowRoot.querySelectorAll(".badge[data-state=checked]").length===12')
            await page.locator('#batch').evaluate('(el) => el.remove()')
            states['http://media-ui.test/lazy.png'] = 'checked'
            await page.evaluate('''() => {
                const img=new Image(320,180);img.id='lazy';img.src='/lazy.png';
                img.style.cssText='position:absolute;top:3000px';document.body.appendChild(img);
            }''')
            await page.wait_for_timeout(100)
            await page.evaluate('''() => {
                const original=Date.now;Date.now=()=>original()+40000;
                document.querySelector('#lazy').scrollIntoView();
            }''')
            await badge.filter(has_text='Checked').wait_for()
            (args.output / 'result.txt').write_text('PASS: checking, filtered, unavailable, inline background hidden; unchanged image URL/layout; underlying click; automatic removal cleanup; bounded long-URL batches; late-visible images checked.\n')
            print((args.output / 'result.txt').read_text())
        except Exception:
            (args.output / 'failure.json').write_text(json.dumps({
                'urls': urls, 'errors': errors,
                'page': await page.content(),
                'status': await page.evaluate('({hidden:document.hidden,ready:document.readyState,badges:document.querySelector("[data-kosheros-ui]")?.shadowRoot.innerHTML})'),
            }, indent=2))
            await page.screenshot(path=str(args.output / 'failure.png'))
            raise
        finally:
            await page.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cdp', default='http://127.0.0.1:9223')
    parser.add_argument('--output', type=Path, required=True)
    asyncio.run(main(parser.parse_args()))
