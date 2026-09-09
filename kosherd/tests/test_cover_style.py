"""The cover style: frost (default) or paint over skin, per account.

Cosmetic — the media level decides what is judged — but it travels to the
proxy with the other per-user settings, so it has to be in the rendered
rules, read by the proxy's policy cache, and honoured by the cover.
"""

import io
import json

import pytest

from kosherd import apply as apply_mod
from kosherd import imageedit
from kosherd.daemon import Daemon
from kosherd.policy import (
    COVER_STYLE_LABELS,
    COVER_STYLES,
    DEFAULT_COVER_STYLE,
    Policy,
    PolicyError,
    UserPolicy,
)

Image = pytest.importorskip("PIL.Image")


def test_every_style_has_a_label_and_frost_is_the_default():
    assert set(COVER_STYLE_LABELS) == set(COVER_STYLES)
    assert DEFAULT_COVER_STYLE == "frost" == imageedit.FROST
    assert "skin" == imageedit.SKIN


def test_the_style_round_trips_and_the_default_stays_out_of_the_file():
    doc = Policy(revision=1, users=[
        UserPolicy(uid=1001, username="a", mode="filtered", cover_style="skin"),
        UserPolicy(uid=1002, username="b", mode="filtered")]).to_dict()
    assert doc["users"][0]["cover_style"] == "skin"
    assert "cover_style" not in doc["users"][1]
    back = Policy.from_dict(doc)
    assert back.user(1001).cover_style == "skin"
    assert back.user(1002).cover_style == "frost"


def test_the_style_reaches_the_proxy(tmp_path, monkeypatch):
    monkeypatch.setattr(apply_mod, "MITM_DIR", tmp_path)
    monkeypatch.setattr(apply_mod, "MITM_RULES_PATH", tmp_path / "rules.json")
    apply_mod.write_mitm_rules(Policy(revision=1, users=[
        UserPolicy(uid=1001, username="a", mode="filtered", cover_style="skin")]))
    doc = json.loads((tmp_path / "rules.json").read_text())
    assert doc["1001"]["cover_style"] == "skin"


def _daemon(users):
    daemon = Daemon.__new__(Daemon)
    daemon.policy = Policy(revision=1, users=users)
    daemon.connection = None
    daemon.applied = 0
    daemon._save_and_apply = lambda: setattr(daemon, "applied", daemon.applied + 1)
    return daemon


def test_setting_a_style_is_applied_so_the_proxy_learns_of_it():
    daemon = _daemon([UserPolicy(uid=1001, username="a", mode="filtered")])
    daemon.impl_SetCoverStyle(1001, "skin")
    assert daemon.policy.user(1001).cover_style == "skin"
    assert daemon.applied == 1
    with pytest.raises(PolicyError):
        daemon.impl_SetCoverStyle(1001, "pixelate")
    with pytest.raises(PolicyError):
        daemon.impl_SetCoverStyle(4242, "skin")


# -- the paint itself --------------------------------------------------------------

SKIN = (224, 172, 105)   # a mid skin tone the YCbCr gate accepts
SHIRT = (30, 60, 200)
GRASS = (10, 160, 10)


def _figure(skin=SKIN, size=300):
    """A green field, a blue shirt block, and a skin-coloured block below it."""
    im = Image.new("RGB", (size, size), GRASS)
    for x in range(100, 200):
        for y in range(60, 150):
            im.putpixel((x, y), SHIRT)
        for y in range(150, 260):
            im.putpixel((x, y), skin)
    out = io.BytesIO()
    im.save(out, "PNG")
    return out.getvalue()


def test_skin_is_painted_solid_and_the_shirt_is_left(tmp_path):
    src = _figure()
    out = Image.open(io.BytesIO(imageedit.cover(src, [(100, 60, 100, 200)], style="skin"))).convert("RGB")
    assert out.getpixel((150, 200)) == imageedit.SKIN_FILL, "skin painted"
    assert out.getpixel((150, 100)) == SHIRT, "clothing left alone"
    assert out.getpixel((20, 20)) == GRASS


def test_the_skin_mask_is_expanded_past_the_edge_of_the_skin():
    src = _figure()
    out = Image.open(io.BytesIO(imageedit.cover(src, [(100, 60, 100, 200)], style="skin"))).convert("RGB")
    # Just outside the skin block (which ended at x=199), still painted.
    assert out.getpixel((204, 200)) == imageedit.SKIN_FILL
    # Well outside, not.
    assert out.getpixel((260, 200)) == GRASS


def test_a_figure_the_colour_gate_cannot_see_is_frosted_instead():
    # A very dark tone the classic YCbCr gate rejects: painting would leave
    # the person visible, so the region is frosted. This is the fairness
    # backstop, and the reason the skin style is never the default.
    src = _figure(skin=(40, 26, 20))
    out = Image.open(io.BytesIO(imageedit.cover(src, [(100, 60, 100, 200)], style="skin"))).convert("RGB")
    assert out.getpixel((150, 200)) != (40, 26, 20), "the figure did not survive"
    assert out.getpixel((150, 200)) != imageedit.SKIN_FILL, "it was frosted, not painted"
    assert out.getpixel((150, 100)) != SHIRT, "frost covers the whole region"


def test_skin_mask_is_none_when_there_is_nothing_to_paint():
    im = Image.new("RGB", (100, 100), GRASS)
    assert imageedit.skin_mask(im) is None


def test_frost_remains_the_behaviour_when_no_style_is_given():
    src = _figure()
    default = imageedit.cover(src, [(100, 60, 100, 200)])
    frost = imageedit.cover(src, [(100, 60, 100, 200)], style="frost")
    a = Image.open(io.BytesIO(default)).convert("RGB").getpixel((150, 200))
    b = Image.open(io.BytesIO(frost)).convert("RGB").getpixel((150, 200))
    assert a != SKIN and b != SKIN
    assert a != imageedit.SKIN_FILL


def test_the_skin_cover_is_not_slow():
    import time

    im = Image.new("RGB", (1600, 1200), GRASS)
    for x in range(400, 900):
        for y in range(200, 1000):
            im.putpixel((x, y), SKIN)
    out = io.BytesIO(); im.save(out, "JPEG", quality=90)
    started = time.monotonic()
    imageedit.cover(out.getvalue(), [(400, 200, 500, 800)], style="skin")
    assert time.monotonic() - started < 1.5
