"""Image state is scoped to the requesting page, bounded and truthful."""
import io
import json

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
