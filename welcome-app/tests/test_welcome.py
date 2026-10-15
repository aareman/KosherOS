"""The welcome, built for real on a virtual display, for an administrator
and for a managed user, plus the once-per-account rule and the image
wiring that switches GNOME's own dialog off."""

from __future__ import annotations

import configparser
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

from kosherwelcome import app  # noqa: E402

ROOT = Path(__file__).parents[2]
FILES = ROOT / "os-image/files"


class FakeClient:
    def __init__(self, settings):
        self.settings = settings

    def get_my_settings(self):
        if isinstance(self.settings, Exception):
            raise self.settings
        return self.settings


def _pump(seconds=0.5):
    deadline = GLib.get_monotonic_time() + int(seconds * 1e6)
    ctx = GLib.MainContext.default()
    while GLib.get_monotonic_time() < deadline:
        ctx.iteration(False)


ADMIN = {"managed": True, "username": "avi", "admin": True, "mode": "unfiltered",
         "time": {"admin": True, "limited": False}}
USER = {"managed": True, "username": "yosef", "admin": False, "mode": "filtered",
        "time": {"admin": False, "limited": True, "daily_minutes": 90}}


def _labels(widget, found=None):
    found = [] if found is None else found
    child = widget.get_first_child()
    while child is not None:
        if isinstance(child, Gtk.Label):
            found.append(child.get_label())
        if isinstance(child, Adw.ActionRow):
            found.append(child.get_title())
            found.append(child.get_subtitle() or "")
        _labels(child, found)
        child = child.get_next_sibling()
    return found


def _window(settings, tmp_path):
    win = app.Window(None, client=FakeClient(settings), home=tmp_path)
    _pump()
    return win


# ---- what each person is told ------------------------------------------------

def test_an_administrator_is_told_admin_things(monkeypatch):
    monkeypatch.setattr(app.GLib, "get_real_name", lambda: "Avi Areman")
    pages = app.pages_for(ADMIN)
    assert pages[0][0] == "Welcome to KosherOS, Avi"
    text = "\n".join(t + b + "".join(n + w for _, n, w in r) for t, b, r in pages)
    assert "you are an administrator" in text
    assert "KosherOS Admin" in text and "Look" in text and "My Filter" in text
    assert "guardian password" in text
    assert "Ask for this page" not in text, "admins are not told how to ask themselves"
    assert "Your time" not in text


def test_a_managed_user_is_told_their_account_is_filtered_and_how_much_time_they_have(monkeypatch):
    monkeypatch.setattr(app.GLib, "get_real_name", lambda: "Unknown")
    pages = app.pages_for(USER)
    assert pages[0][0] == "Welcome, yosef", "no full name: the username"
    text = "\n".join(t + b + "".join(n + w for _, n, w in r) for t, b, r in pages)
    assert "your account is filtered" in text
    assert "Ask for this page" in text
    assert "1 h 30 min on this computer each day" in text
    assert "KosherOS Admin" not in text, "nothing an admin would open"


def test_no_time_limit_means_no_time_row():
    pages = app.pages_for({**USER, "time": {"admin": False, "limited": False}})
    assert all(n != "Your time" for _, _, rows in pages for _, n, _ in rows)


def test_every_row_has_a_name_and_a_sentence():
    for pages in (app.ADMIN_PAGES, app.USER_PAGES):
        for _, _, rows in pages:
            for icon, name, words in rows:
                assert icon.endswith("-symbolic") and name and words.endswith(".")


# ---- the window --------------------------------------------------------------

def test_the_admin_window_walks_its_pages_and_ends_on_open_admin(tmp_path, monkeypatch):
    monkeypatch.setattr(app.GLib, "get_real_name", lambda: "Avi")
    win = _window(ADMIN, tmp_path)
    assert len(win.pages) == 3
    assert not win.back.get_visible()
    assert win.next.get_label() == "Next"
    assert not win.admin_button.get_visible()
    win._go(+1); win._go(+1)
    _pump(0.8)
    win._buttons()
    assert win.back.get_visible()
    assert win.next.get_label() == "Done"
    assert win.admin_button.get_visible(), "the last admin page offers the admin app"
    assert not app.seen(tmp_path)
    win._go(+1)
    assert app.seen(tmp_path), "Done stamps the account"


def test_the_user_window_never_offers_the_admin_app(tmp_path, monkeypatch):
    monkeypatch.setattr(app.GLib, "get_real_name", lambda: "Yosef")
    win = _window(USER, tmp_path)
    win._go(+1); win._go(+1)
    _pump(0.8)
    win._buttons()
    assert win.next.get_label() == "Done"
    assert not win.admin_button.get_visible()
    labels = _labels(win)
    assert any("filtered" in lab for lab in labels)


def test_closing_the_window_counts_as_seen(tmp_path, monkeypatch):
    monkeypatch.setattr(app.GLib, "get_real_name", lambda: "Yosef")
    win = _window(USER, tmp_path)
    win.emit("close-request")
    assert app.seen(tmp_path)


def test_without_the_daemon_the_general_welcome_is_shown(tmp_path, monkeypatch):
    monkeypatch.setattr(app.GLib, "get_real_name", lambda: "Unknown")
    win = _window(RuntimeError("GDBus.Error: gone"), tmp_path)
    assert win.pages, "a page rather than a blank window"
    assert not win.admin_button.get_visible()


# ---- once per account --------------------------------------------------------

def test_main_leaves_at_once_when_the_account_has_seen_it(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "seen", lambda: True)
    ran = []
    monkeypatch.setattr(app.Application, "run", lambda self, _a: ran.append(1) or 0)
    assert app.main([]) == 0
    assert ran == []
    assert app.main(["--again"]) == 0
    assert ran == [1]


# ---- the image ----------------------------------------------------------------

def test_the_image_switches_gnomes_welcome_off():
    containerfile = (ROOT / "os-image/Containerfile").read_text()
    remove = containerfile.split("--setopt=protected_packages= remove", 1)[0]
    assert "gnome-tour" in remove.rsplit("RUN for p in", 1)[1], "the tour is removed with the rest"
    cp = configparser.ConfigParser()
    cp.read_string((FILES / "etc/dconf/db/local.d/20-kosheros-desktop").read_text())
    assert cp["org/gnome/shell"]["welcome-dialog-last-shown-version"] == "'99'"
    locks = (FILES / "etc/dconf/db/local.d/locks/10-kosheros-locks").read_text()
    assert "/org/gnome/shell/welcome-dialog-last-shown-version" in locks


def test_the_welcome_autostarts_in_gnome_and_can_be_reopened():
    auto = configparser.ConfigParser(interpolation=None)
    auto.read_string((FILES / "etc/xdg/autostart/org.kosherlinux.Welcome.desktop").read_text())
    entry = auto["Desktop Entry"]
    assert entry["Exec"] == "kosher-welcome"
    assert entry["OnlyShowIn"] == "GNOME;"
    assert entry["NoDisplay"] == "true", "the autostart entry is not an app in the menu"
    launcher = configparser.ConfigParser(interpolation=None)
    launcher.read_string((FILES / "usr/share/applications/org.kosherlinux.Welcome.desktop").read_text())
    assert launcher["Desktop Entry"]["Exec"] == "kosher-welcome --again"
    assert "NoDisplay" not in launcher["Desktop Entry"]


def test_the_image_installs_the_app_and_just_test_runs_its_suite():
    containerfile = (ROOT / "os-image/Containerfile").read_text()
    assert "COPY welcome-app/ /tmp/welcome-app/" in containerfile
    assert "/tmp/welcome-app/" in containerfile.split("pip install", 1)[1]
    assert "welcome-app/tests" in (ROOT / "Justfile").read_text()
