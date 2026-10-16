"""Building Look for real, on a virtual display, against fakes for the
daemon and the blur settings.

The window is what the tests hold: which desktop is marked, that choosing
one reaches the daemon for THIS account and the running session when it
can, and that nothing here writes until the person moves something.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

gi = pytest.importorskip("gi")
try:
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Adw, GLib, Gtk
except ValueError:  # pragma: no cover - no typelibs on this machine
    pytest.skip("GTK4/libadwaita not available", allow_module_level=True)

if not Gtk.init_check():  # pragma: no cover - no display
    pytest.skip("no display", allow_module_level=True)
Adw.init()

from kosherd.policy import LAYOUTS  # noqa: E402
from kosherlook import app  # noqa: E402

ROOT = Path(__file__).parents[2]


class FakeClient:
    def __init__(self, layout="classic", fail=False):
        self.layout = layout
        self.fail = fail
        self.chosen = []

    def my_layout(self):
        return self.layout

    def set_my_layout(self, layout):
        if self.fail:
            raise RuntimeError("GDBus.Error:org.kosherlinux.Error: not now")
        self.chosen.append(layout)
        self.layout = layout


PIPELINES = (
    "{'pipeline_default': {'name': <'Default'>, 'effects': <[<{'type': <'native_static_gaussian_blur'>, "
    "'id': <'d'>, 'params': <{'unscaled_radius': <20.0>, 'brightness': <0.8>}>}>]>}, "
    "'kosher_panel': {'name': <'KosherOS taskbar'>, 'effects': <[<{'type': <'native_static_gaussian_blur'>, "
    "'id': <'p'>, 'params': <{'unscaled_radius': <20.0>, 'brightness': <0.8>}>}>]>}, "
    "'kosher_windows': {'name': <'KosherOS windows'>, 'effects': <[<{'type': <'native_static_gaussian_blur'>, "
    "'id': <'w'>, 'params': <{'unscaled_radius': <20.0>, 'brightness': <0.8>}>}>, "
    "<{'type': <'corner'>, 'id': <'c'>, 'params': <{'radius': <15>}>}>]>}}"
)


class FakeSettings:
    """Enough of Gio.Settings for the Glass class: typed get/set/reset over
    a dict, with the defaults the image ships."""

    def __init__(self, defaults):
        self.defaults = dict(defaults)
        self.values = dict(defaults)

    def get_boolean(self, k): return self.values[k]
    def get_string(self, k): return self.values[k]
    def get_int(self, k): return self.values[k]
    def get_value(self, k): return self.values[k]
    def set_boolean(self, k, v): self.values[k] = v
    def set_int(self, k, v): self.values[k] = v
    def set_value(self, k, v):
        assert v.get_type_string() == "a{sa{sv}}"
        self.values[k] = v
    def reset(self, k): self.values[k] = self.defaults[k]


def _glass(panel=True, windows=True):
    root = FakeSettings({"pipelines": GLib.Variant.parse(None, PIPELINES, None, None)})
    return app.Glass(root=root, parts={
        "panel": FakeSettings({"blur": panel, "pipeline": "kosher_panel"}),
        "applications": FakeSettings({"blur": windows, "pipeline": "kosher_windows",
                                      "opacity": 225}),
    })


def _blur(glass):
    return [p.values["blur"] for p in glass.parts.values()]


def _pump(seconds=0.5):
    """Let the worker thread and the idle callbacks it queues run."""
    deadline = GLib.get_monotonic_time() + int(seconds * 1e6)
    ctx = GLib.MainContext.default()
    while GLib.get_monotonic_time() < deadline:
        ctx.iteration(False)


def _window(client=None, glass=None, in_niri=False):
    applied = []
    win = app.Window(None, client=client or FakeClient(), glass=glass or _glass(),
                     apply=applied.append, in_niri=in_niri)
    _pump()
    return win, applied


# ---- the desktops ----------------------------------------------------------

def test_the_desktops_offered_are_exactly_the_layouts_kosherd_knows():
    assert tuple(k for k, _, _ in app.DESKTOPS) == LAYOUTS


def test_the_current_desktop_is_marked_and_classic_needs_no_sign_out():
    win, _ = _window(FakeClient("classic"))
    assert win.checks["classic"].get_active()
    assert not win.checks["tiling"].get_active()
    assert not win.banner.get_revealed()


def test_choosing_tiling_reaches_the_daemon_and_the_running_session():
    client = FakeClient("classic")
    win, applied = _window(client)
    win.checks["tiling"].set_active(True)
    _pump()
    assert client.chosen == ["tiling"]
    assert applied == ["tiling"], "a GNOME desktop switches in place"
    assert not win.banner.get_revealed()


def test_choosing_advanced_waits_for_the_next_sign_in():
    client = FakeClient("classic")
    win, applied = _window(client)
    win.checks["advanced"].set_active(True)
    _pump()
    assert client.chosen == ["advanced"]
    assert applied == [], "a different session cannot be switched to in place"
    assert win.banner.get_revealed()


def test_under_niri_the_gnome_desktops_wait_too():
    client = FakeClient("advanced")
    win, applied = _window(client, in_niri=True)
    assert not win.banner.get_revealed(), "running what was chosen: nothing to announce"
    win.checks["classic"].set_active(True)
    _pump()
    assert client.chosen == ["classic"]
    assert applied == [], "kosher-layout writes GNOME settings; this is not GNOME"
    assert win.banner.get_revealed()


def test_a_refused_choice_goes_back_to_what_it_was():
    client = FakeClient("classic", fail=True)
    win, applied = _window(client)
    win.checks["tiling"].set_active(True)
    _pump()
    assert client.chosen == []
    assert applied == []
    assert win.checks["classic"].get_active()
    assert win.layout == "classic"


def test_the_daemon_being_away_is_a_page_not_a_crash():
    class Away:
        def my_layout(self):
            raise RuntimeError("GDBus.Error:org.freedesktop.DBus.Error.ServiceUnknown: gone")
    win = app.Window(None, client=Away(), glass=_glass(), apply=lambda _l: None, in_niri=False)
    _pump()
    page = win.view.get_content()
    assert isinstance(page, Adw.StatusPage)
    assert page.get_title() == app.TEXT["error_title"]


# ---- the glass --------------------------------------------------------------

def test_one_switch_blurs_the_taskbar_and_the_windows_together():
    glass = _glass(panel=True, windows=False)
    win, _ = _window(glass=glass)
    assert win.glass_group.get_visible()
    assert win.blur_row.get_active(), "on when either part is blurred"
    win.blur_row.set_active(False)
    assert _blur(glass) == [False, False]
    assert not win.strength_row.get_sensitive(), "nothing to tune with the blur off"
    win.blur_row.set_active(True)
    assert _blur(glass) == [True, True]
    assert win.strength_row.get_sensitive() and win.transparency_row.get_sensitive()


def test_opening_look_writes_nothing():
    glass = _glass(panel=False, windows=True)
    _window(glass=glass)
    assert _blur(glass) == [False, True]
    assert glass.root.values == glass.root.defaults
    assert glass.parts["applications"].values["opacity"] == 225


def _radii(glass):
    found = {}
    for name, body in glass.root.values["pipelines"].unpack().items():
        found[name] = [e["params"] for e in body["effects"]]
    return found


def test_how_much_sets_the_radius_of_the_taskbar_and_window_blur_only():
    glass = _glass()
    win, _ = _window(glass=glass)
    assert win.strength.get_value() == 38, "radius 20 of 2..50"
    win.strength.set_value(100)
    radii = _radii(glass)
    assert radii["kosher_panel"][0]["unscaled_radius"] == 50.0
    assert radii["kosher_windows"][0]["unscaled_radius"] == 50.0
    assert radii["pipeline_default"][0]["unscaled_radius"] == 20.0, "popups keep theirs"
    # Everything else in the pipeline keeps its value and its type.
    assert radii["kosher_windows"][0]["brightness"] == 0.8
    corner = glass.root.values["pipelines"].lookup_value("kosher_windows", None)
    assert "<{'radius': <15>}>" in corner.print_(False), "the corner radius stays an int"


def test_transparency_is_the_windows_opacity_and_stays_readable():
    glass = _glass()
    win, _ = _window(glass=glass)
    assert win.transparency.get_value() == 12, "225 of 255 is 12% see-through"
    assert win.transparency.get_adjustment().get_upper() == 50
    win.transparency.set_value(40)
    assert glass.parts["applications"].values["opacity"] == 153
    glass.transparency = 0.9
    assert glass.parts["applications"].values["opacity"] == 128, "never past half"


def test_back_to_the_kosheros_look_unsets_everything():
    glass = _glass()
    win, _ = _window(glass=glass)
    win.strength.set_value(90)
    win.transparency.set_value(45)
    win.blur_row.set_active(False)
    win.reset_button.emit("clicked")
    assert glass.root.values == glass.root.defaults
    assert all(p.values == p.defaults for p in glass.parts.values())
    assert win.blur_row.get_active()
    assert win.strength.get_value() == 38 and win.transparency.get_value() == 12


def test_under_niri_there_is_no_blur_switch():
    win, _ = _window(FakeClient("advanced"), in_niri=True)
    assert not win.glass_group.get_visible()


def test_without_the_extension_the_switch_is_left_out():
    win, _ = _window(glass=app.Glass(root=None, parts={"panel": None, "applications": None}))
    assert win.glass_group is None
    assert win.checks["classic"].get_active()


# ---- the image --------------------------------------------------------------

def test_the_image_installs_the_app_and_its_launcher():
    containerfile = (ROOT / "os-image/Containerfile").read_text()
    assert "COPY look-app/ /tmp/look-app/" in containerfile
    assert "/tmp/look-app/" in containerfile.split("pip install", 1)[1]
    entry = (ROOT / "os-image/files/usr/share/applications/org.kosherlinux.Look.desktop").read_text()
    assert "Exec=kosher-look" in entry
    assert "Icon=preferences-desktop-appearance" in entry
    pyproject = (ROOT / "look-app/pyproject.toml").read_text()
    assert re.search(r'kosher-look = "kosherlook\.app:main"', pyproject)


def test_the_test_suite_is_run_by_just_test():
    justfile = (ROOT / "Justfile").read_text()
    assert "look-app/tests" in justfile
