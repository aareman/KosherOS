"""The build-time rasters: the wordmark must survive every screen shape.

The wallpaper is 3:2 with "KosherOS" painted into its top-left corner.
GNOME's zoom cropped it from the centre, and on the 16:9 and 4:3 screens
people actually have the word was cut off. The fix is a crop per shape,
anchored top-left, offered through a background XML. And the login screen
needs the name beside the logo, because GDM's logo key is one image drawn
at native size and a small faint ellipse says nothing on its own.
"""

import configparser
import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
FILES = ROOT / "os-image/files"
BRANDING = ROOT / "branding"

PIL = pytest.importorskip("PIL")
from PIL import Image  # noqa: E402

spec = importlib.util.spec_from_file_location("rasters", BRANDING / "rasters.py")
rasters = importlib.util.module_from_spec(spec)
sys.modules["rasters"] = rasters
spec.loader.exec_module(rasters)


def _marked(w=300, h=200):
    """A test picture with a distinct top-left pixel and bottom-right pixel."""
    im = Image.new("RGB", (w, h), (200, 200, 200))
    im.putpixel((0, 0), (255, 0, 0))
    im.putpixel((w - 1, h - 1), (0, 0, 255))
    return im


@pytest.mark.parametrize("aspect,_adv", rasters.ASPECTS)
def test_every_crop_keeps_the_top_left_corner(aspect, _adv):
    out = rasters.top_left_crop(_marked(), aspect)
    aw, ah = aspect
    # Integer pixel rounding on a small test picture; the real one is 4978 wide.
    assert abs(out.width / out.height - aw / ah) < 0.02
    assert out.getpixel((0, 0)) == (255, 0, 0), "the wordmark corner must survive"
    assert out.width <= 300 and out.height <= 200


def test_a_wider_target_trims_the_bottom_and_a_narrower_one_the_right():
    src = _marked(300, 200)  # 3:2
    wide = rasters.top_left_crop(src, (16, 9))
    assert wide.width == 300 and wide.height < 200
    narrow = rasters.top_left_crop(src, (4, 3))
    assert narrow.height == 200 and narrow.width < 300


def test_the_shapes_cover_laptops_monitors_and_the_vm():
    aspects = {a for a, _ in rasters.ASPECTS}
    for needed in ((16, 9), (16, 10), (4, 3), (3, 2)):
        assert needed in aspects


def test_the_background_xml_lists_every_crop_for_gnome():
    entries = [(adv, Path(f"/x/{a[0]}x{a[1]}.png")) for a, adv in rasters.ASPECTS]
    xml = rasters.background_xml(entries)
    assert xml.startswith('<?xml version="1.0"?>')
    assert xml.count("<size ") == len(rasters.ASPECTS)
    assert '<size width="1920" height="1080">/x/16x9.png</size>' in xml
    assert "<static>" in xml and "</background>" in xml


def test_the_desktop_points_at_the_xml_not_the_single_picture():
    cp = configparser.ConfigParser()
    cp.read_string((FILES / "etc/dconf/db/local.d/10-kosheros-branding").read_text())
    for section in ("org/gnome/desktop/background", "org/gnome/desktop/screensaver"):
        assert cp[section]["picture-uri"] == \
            "'file:///usr/share/backgrounds/kosheros/kosheros.xml'"
    assert cp["org/gnome/desktop/background"]["picture-uri-dark"] == \
        "'file:///usr/share/backgrounds/kosheros/kosheros.xml'"


def test_the_login_lockup_has_the_name_beside_the_logo():
    logo = Image.new("RGBA", (400, 300), (120, 100, 80, 255))
    strip = rasters.lockup(logo, height=72)
    assert strip.height == 72
    # Logo square, a gap, then the word: much wider than tall.
    assert strip.width > 72 * 2
    # Something dark was drawn to the right of the logo square.
    right_half = strip.crop((72 + 14, 0, strip.width, 72))
    dark = [p for p in right_half.getdata() if p[3] > 0 and p[0] < 120]
    assert dark, "no wordmark pixels were drawn"


def test_the_fitted_logo_is_square_and_keeps_aspect():
    logo = Image.new("RGBA", (400, 200), (0, 0, 0, 255))
    out = rasters.fitted(logo, 100)
    assert out.size == (100, 100)
    assert out.getpixel((50, 10)) == (0, 0, 0, 0), "padding above a wide logo"
    assert out.getpixel((50, 50))[3] == 255


def test_the_build_runs_the_script_on_real_artwork(tmp_path):
    # The whole thing, on the actual files, into a scratch root — what the
    # Containerfile does with --out /.
    made = rasters.build(BRANDING, tmp_path)
    names = {p.relative_to(tmp_path).as_posix() for p in made}
    assert "usr/share/pixmaps/kosheros-logo-login.png" in names
    assert "usr/share/plymouth/themes/kosheros/logo.png" in names
    assert "usr/share/backgrounds/kosheros/kosheros.xml" in names
    assert "usr/share/backgrounds/kosheros/sizes/kosheros-16x9.png" in names
    xml = (tmp_path / "usr/share/backgrounds/kosheros/kosheros.xml").read_text()
    assert "/usr/share/backgrounds/kosheros/sizes/kosheros-4x3.png" in xml
