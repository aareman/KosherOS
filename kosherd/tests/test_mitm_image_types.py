"""Mislabelled pictures must enter image policy, including decoder gaps."""

import pytest

from test_mitm_addon import addon as addon, _filt, _headers_hook, _hflow


@pytest.mark.parametrize("extension", [
    "gif", "png", "apng", "jpg", "jpeg", "jfif", "pjp", "pjpeg",
    "webp", "avif", "bmp", "ico", "svg", "tif", "tiff", "jxl", "heic", "heif",
])
@pytest.mark.parametrize("content_type", [
    "application/octet-stream", "binary/octet-stream; charset=binary",
])
def test_octet_stream_image_extensions_are_recognised(addon, extension, content_type):
    flow = _hflow(content_type, length=10_000,
                  url=f"https://cdn.example.com/picture.{extension.upper()}?download=1")
    assert addon._is_image(flow)
    assert not addon._is_video(flow)


@pytest.mark.parametrize("extension", ["svg", "jxl"])
def test_unsupported_images_are_held_for_policy_not_streamed(addon, extension):
    flow = _hflow("application/octet-stream", length=10_000,
                  url=f"https://cdn.example.com/picture.{extension}")
    _headers_hook(_filt(addon, media="immodest"), flow)
    assert not getattr(flow.response, "stream", False)


@pytest.mark.parametrize("url", [
    "https://cdn.example.com/download.bin?name=picture.svg",
    "https://cdn.example.com/picture.jxl/download",
    "https://cdn.example.com/picture.svg.zip",
])
def test_non_image_downloads_still_stream(addon, url):
    flow = _hflow("application/octet-stream", length=10_000, url=url)
    assert not addon._is_image(flow)
    _headers_hook(_filt(addon, media="immodest"), flow)
    assert flow.response.stream is True


def test_extension_does_not_override_explicit_non_image_type(addon):
    assert not addon._is_image(_hflow("text/html", url="https://example.com/page.svg"))


def test_image_type_needs_no_recognised_extension(addon):
    assert addon._is_image(_hflow("image/jxl", url="https://example.com/image"))
