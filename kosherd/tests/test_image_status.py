"""Image state is scoped to the requesting page, bounded and truthful."""
import asyncio
import io
import json
from types import SimpleNamespace

import pytest
from PIL import Image

from test_mitm_addon import addon as addon
from test_page_filter import _filter, _flow


def request_status(addon, filt, urls, host='shop.example', uid=1001, token=True):
    flow = _flow(f'https://{host}{addon.IMAGE_STATUS_PATH}', method='POST',
                 body=json.dumps({'urls': urls}).encode())
    if token:
        flow.request.headers[addon.PAGE_TOKEN_HEADER] = filt._page_token(
            uid, flow.request.pretty_host)
    assert filt._serve_page_filter(flow, uid)
    return flow.response


def test_image_states_are_scoped_to_account_and_referring_origin(addon):
    filt = _filter(addon, media='immodest')
    image = _flow('https://cdn.example/photo.jpg', headers={'referer': 'https://shop.example/item'})
    filt._image_status(image, 'checking')
    response = request_status(addon, filt, [image.request.pretty_url])
    assert json.loads(response.content)['images'][image.request.pretty_url] == 'checking'
    filt._image_status(image, 'filtered')
    assert json.loads(request_status(addon, filt, [image.request.pretty_url]).content)['images'][image.request.pretty_url] == 'filtered'
    assert json.loads(request_status(addon, filt, [image.request.pretty_url], host='other.example').content)['images'] == {}
    assert json.loads(request_status(addon, filt, [image.request.pretty_url], uid=1002).content)['images'] == {}
    assert request_status(addon, filt, [image.request.pretty_url], token=False).status_code == 403


def test_missing_referrer_and_expired_observations_are_not_exposed(addon, monkeypatch):
    filt = _filter(addon, media='all')
    image = _flow('https://cdn.example/photo.jpg')
    filt._image_status(image, 'hidden')
    assert json.loads(request_status(addon, filt, [image.request.pretty_url]).content)['images'] == {}
    image.request.headers['referer'] = 'https://shop.example/'
    filt._image_status(image, 'unavailable')
    now = addon.time.monotonic()
    monkeypatch.setattr(addon.time, 'monotonic', lambda: now + filt.IMAGE_STATUS_TTL + 1)
    assert json.loads(request_status(addon, filt, [image.request.pretty_url]).content)['images'] == {}


def image_request(addon, filt):
    """Run the request hook before any upstream response headers arrive."""
    flow = _flow('https://cdn.example/photo.jpg',
                 headers={'referer': 'https://shop.example/item'})
    filt._uid_of = lambda flow: 1001
    filt._blocked_categories_of = lambda host, uid: set()
    filt._request(flow)
    return flow


def test_new_request_invalidates_old_result_before_upstream_headers(addon):
    filt = _filter(addon, media='immodest')
    old = image_request(addon, filt)
    assert '_image_statuses' not in filt.__dict__
    filt._image_status(old, 'checked')
    other = _flow(old.request.pretty_url, headers={'referer': 'https://other.example/'})
    filt._image_status(other, 'filtered')

    current = image_request(addon, filt)
    assert current.response is None
    assert json.loads(request_status(addon, filt, [current.request.pretty_url]).content)['images'] == {}
    assert json.loads(request_status(addon, filt, [other.request.pretty_url], host='other.example').content)['images'] == {other.request.pretty_url: 'filtered'}
    current.response = SimpleNamespace(status_code=200, headers={'content-type': 'image/jpeg'})
    filt._recently_clean = lambda flow, level: False
    asyncio.run(filt._responseheaders(current))
    assert current.metadata['kosher_image_status_level'] == 'immodest'
    assert json.loads(request_status(addon, filt, [current.request.pretty_url]).content)['images'] == {current.request.pretty_url: 'checking'}


@pytest.mark.parametrize('state', ['checked', 'filtered', 'hidden', 'unavailable'])
@pytest.mark.parametrize('content_type', [None, 'image/jpeg'])
def test_304_preserves_cached_image_state_without_filtering_empty_body(addon, state, content_type):
    filt = _filter(addon, media='immodest')
    old = image_request(addon, filt)
    filt._image_status(old, state)
    previous = next(iter(filt._image_statuses.values()))
    current = image_request(addon, filt)
    headers = {'etag': 'original'}
    if content_type:
        headers['content-type'] = content_type
    current.response = SimpleNamespace(status_code=304, headers=headers.copy(), content=b'', stream=False)

    asyncio.run(filt._responseheaders(current))
    asyncio.run(filt._response(current))

    assert current.response.status_code == 304
    assert current.response.headers == headers
    assert current.response.content == b''
    assert next(iter(filt._image_statuses.values())) == previous  # no renewed TTL
    assert 'kosher_previous_image_status' not in current.metadata
    assert json.loads(request_status(addon, filt, [current.request.pretty_url]).content)['images'] == {current.request.pretty_url: state}


@pytest.mark.parametrize('change', ['expired', 'level', 'checking'])
def test_304_does_not_restore_stale_or_unfinished_state(addon, monkeypatch, change):
    filt = _filter(addon, media='immodest')
    old = image_request(addon, filt)
    filt._image_status(old, 'checking' if change == 'checking' else 'filtered')
    current = image_request(addon, filt)
    if change == 'level':
        filt.policy.media_level_for = lambda uid: 'all'
    elif change == 'expired':
        now = addon.time.monotonic()
        monkeypatch.setattr(addon.time, 'monotonic', lambda: now + filt.IMAGE_STATUS_TTL + 1)
    current.response = SimpleNamespace(status_code=304, headers={})
    asyncio.run(filt._responseheaders(current))
    assert json.loads(request_status(addon, filt, [current.request.pretty_url]).content)['images'] == {}


def test_304_does_not_replace_newer_concurrent_result(addon):
    filt = _filter(addon, media='immodest')
    old = image_request(addon, filt)
    filt._image_status(old, 'checked')
    current = image_request(addon, filt)
    concurrent = image_request(addon, filt)
    filt._image_status(concurrent, 'hidden')
    current.response = SimpleNamespace(status_code=304, headers={})
    asyncio.run(filt._responseheaders(current))
    assert json.loads(request_status(addon, filt, [current.request.pretty_url]).content)['images'] == {current.request.pretty_url: 'hidden'}


def test_status_from_another_media_level_is_not_reported(addon):
    filt = _filter(addon, media='immodest')
    image = image_request(addon, filt)
    filt._image_status(image, 'checked')
    filt.policy.media_level_for = lambda uid: 'all'
    assert json.loads(request_status(addon, filt, [image.request.pretty_url]).content)['images'] == {}


def test_status_records_level_used_by_inflight_image_decision(addon):
    filt = _filter(addon, media='immodest')
    image = image_request(addon, filt)
    image.metadata['kosher_image_status_level'] = 'nsfw'  # earlier headers
    image.response = SimpleNamespace(content=b'image')
    filt._image_initial_action = lambda flow, uid, level: 'judge'
    filt._image_verdict_action = lambda flow, uid, level, verdict: 'pass'
    filt._publish_image_status = lambda: None

    async def verdict(body):
        await asyncio.sleep(0)
        filt.policy.media_level_for = lambda uid: 'all'
        return object()

    filt.vision = SimpleNamespace(verdict_async=verdict)
    asyncio.run(filt._filter_image_async(image, 1001))
    assert image.metadata['kosher_image_status_level'] == 'immodest'
    assert json.loads(request_status(addon, filt, [image.request.pretty_url]).content)['images'] == {}
    filt.policy.media_level_for = lambda uid: 'immodest'
    assert json.loads(request_status(addon, filt, [image.request.pretty_url]).content)['images'] == {image.request.pretty_url: 'checked'}


def test_video_304_still_applies_video_policy(addon):
    filt = _filter(addon, media='all')
    filt._blocked_categories_of = lambda host, uid: set()
    video = _flow('https://cdn.example/movie.mp4')
    video.response = SimpleNamespace(status_code=304, headers={'content-type': 'video/mp4'})
    refused = []

    async def refuse(flow, *, before_body=False):
        refused.append(before_body)
        flow.response.status_code = 403

    filt._refuse_video_async = refuse
    asyncio.run(filt._responseheaders(video))
    assert refused == [True]
    assert video.response.status_code == 403


@pytest.mark.parametrize('source, other', [
    ('[::1]:9000', '[::1:9000]'),
    ('[::1:9000]', '[::1]:9000'),
    ('shop.example:0', 'shop.example'),
])
def test_image_status_distinguishes_ipv6_addresses_and_explicit_ports(addon, source, other):
    filt = _filter(addon, media='immodest')
    image = _flow('https://cdn.example/photo.jpg',
                  headers={'referer': f'https://{source}/item'})
    filt._image_status(image, 'filtered')
    own = request_status(addon, filt, [image.request.pretty_url], host=source)
    unrelated = request_status(addon, filt, [image.request.pretty_url], host=other)
    assert own.status_code == unrelated.status_code == 200
    assert json.loads(own.content)['images'] == {image.request.pretty_url: 'filtered'}
    assert json.loads(unrelated.content)['images'] == {}


@pytest.mark.parametrize('host', ['shop.example', '[::1]'])
def test_image_status_default_port_is_the_same_origin(addon, host):
    filt = _filter(addon, media='immodest')
    image = _flow('https://cdn.example/photo.jpg',
                  headers={'referer': f'https://{host}:443/item'})
    filt._image_status(image, 'checked')
    response = request_status(addon, filt, [image.request.pretty_url], host=host)
    assert response.status_code == 200
    assert json.loads(response.content)['images'] == {image.request.pretty_url: 'checked'}


def test_image_status_memory_and_request_size_are_bounded(addon):
    filt = _filter(addon, media='all')
    filt.MAX_IMAGE_STATUSES = 2
    for i in range(3):
        filt._image_status(_flow(f'https://cdn.example/{i}.jpg',
                           headers={'referer': 'https://shop.example/'}), 'hidden')
    assert len(filt._image_statuses) == 2
    filt._image_status(_flow('https://cdn.example/' + 'x' * 2100,
                            headers={'referer': 'https://shop.example/'}), 'hidden')
    assert len(filt._image_statuses) == 2
    assert all(len(key[2]) <= 2048 for key in filt._image_statuses)
    assert request_status(addon, filt, ['https://cdn.example/x'] * 65).status_code == 400
    assert request_status(addon, filt, ['x' * 21000]).status_code == 413


def test_images_only_policy_injects_status_script_without_text_filter(addon):
    filt = _filter(addon, language='off', media='all')
    flow = _flow(html='<html><body><img src="x.jpg"></body></html>')
    filt._inject_page_script(flow, 'off', judge=False)
    assert 'data-kosheros-images="1"' in flow.response.text
    assert 'data-kosheros-judge="0"' in flow.response.text
    unfiltered = _filter(addon, language='off', media='none')
    flow = _flow(html='<html><body>hello</body></html>')
    unfiltered._inject_page_script(flow, 'off', judge=False)
    assert addon.SCRIPT_PATH not in flow.response.text


def test_emergency_image_is_opaque_neutral_not_translucent_red(addon):
    with Image.open(io.BytesIO(addon.BLANK_PNG)) as image:
        assert image.size == (1, 1)
        assert image.convert('RGBA').getpixel((0, 0)) == (214, 214, 214, 255)


def test_explicit_pictures_allowance_streams_without_claiming_a_check(addon):
    import asyncio

    filt = _filter(addon, media='all')
    flow = _flow('https://cdn.example/photo.jpg',
                 headers={'referer': 'https://shop.example/item'})
    flow.metadata['kosher_pictures'] = True
    from types import SimpleNamespace
    flow.response = SimpleNamespace(status_code=200,
                                    headers={'content-type': 'image/jpeg'},
                                    content=b'image', stream=False)
    asyncio.run(filt._responseheaders(flow))
    assert flow.response.stream is True
    response = request_status(addon, filt, [flow.request.pretty_url])
    assert json.loads(response.content)['images'] == {
        flow.request.pretty_url: 'not_checked'}
