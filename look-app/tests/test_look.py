"""Building Look for real, on a virtual display, against fakes for the
daemon.

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


def _pump(seconds=0.5):
    """Let the worker thread and the idle callbacks it queues run."""
    deadline = GLib.get_monotonic_time() + int(seconds * 1e6)
    ctx = GLib.MainContext.default()
    while GLib.get_monotonic_time() < deadline:
        ctx.iteration(False)


def _window(client=None, in_niri=False):
    applied = []
    win = app.Window(None, client=client or FakeClient(),
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
    win = app.Window(None, client=Away(), apply=lambda _l: None, in_niri=False)
    _pump()
    page = win.view.get_content()
    assert isinstance(page, Adw.StatusPage)
    assert page.get_title() == app.TEXT["error_title"]


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
