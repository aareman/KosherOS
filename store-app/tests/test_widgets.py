"""The Store's shelves, its grid, and the icons on it, built for real.

Every UI bug this project has shipped was a widget that threw when it was
constructed, which a source-reading test cannot see. These build the real
window against the pretend daemon, on a virtual display.
"""

from __future__ import annotations

import pytest

gi = pytest.importorskip("gi")
try:
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Adw, Gtk
except ValueError:  # pragma: no cover - no typelibs here
    pytest.skip("GTK4/libadwaita not available", allow_module_level=True)

if not Gtk.init_check():  # pragma: no cover - no display
    pytest.skip("no display", allow_module_level=True)
Adw.init()

from kosherd.demo import DemoClient  # noqa: E402
from kosherstore import app as store  # noqa: E402


def drain():
    from gi.repository import GLib

    context = GLib.MainContext.default()
    for _ in range(200):
        while context.pending():
            context.iteration(False)


@pytest.fixture
def window(monkeypatch):
    monkeypatch.setattr(store, "DaemonClient", DemoClient)
    win = store.Window()
    drain()
    return win


def _children(box):
    found = []
    child = box.get_first_child()
    while child is not None:
        found.append(child)
        child = child.get_next_sibling()
    return found


def test_the_window_opens_on_a_grid_of_cards(window):
    assert window.stack.get_visible_child_name() == "grid"
    cards = _children(window.grid)
    assert len(cards) == len(window.catalog)
    first = cards[0].get_child()
    assert isinstance(first, store.AppCard)
    assert first.button.get_label() in ("Install", "Remove")


def test_every_app_lands_on_exactly_one_shelf():
    from kosherd.demo import DemoClient as C

    for app in C().catalog:
        key = store.shelf_of(app)
        claiming = [k for k, _l, claims in store.SHELVES
                    if set(app.get("categories") or ()) & set(claims)]
        assert key == (claiming[0] if claiming else "other")


def test_an_app_with_no_categories_falls_to_everything_else():
    assert store.shelf_of({"ref": "x", "name": "X"}) == "other"
    assert store.shelf_of({"ref": "x", "categories": ["Nonsense"]}) == "other"


def test_the_shelves_are_only_the_ones_with_apps_on_them(window):
    labels = [b.get_label().rsplit("  ", 1)[0] for b in _children(window.shelf_box)]
    assert labels[:2] == ["All", "Installed"]
    assert "Internet" in labels and "Games" in labels
    # Nothing in the demo catalogue is unclassified, so that shelf is absent.
    assert "Everything else" not in labels
    counts = window.shelf_counts()
    assert counts["all"] == len(window.catalog)
    assert counts["installed"] == len(window.installed)


def test_choosing_a_shelf_narrows_the_grid(window):
    games = next(b for b in _children(window.shelf_box)
                 if b.get_label().startswith("Games"))
    games.set_active(True)
    drain()
    assert window.shelf == "games"
    shown = window.shown_apps()
    assert shown and all(store.shelf_of(a) == "games" for a in shown)
    assert len(_children(window.grid)) == len(shown)


def test_the_installed_shelf_shows_what_is_installed(window):
    installed = next(b for b in _children(window.shelf_box)
                     if b.get_label().startswith("Installed"))
    installed.set_active(True)
    drain()
    shown = window.shown_apps()
    assert {a["ref"] for a in shown} <= window.installed
    assert all(c.get_child().button.get_label() == "Remove"
               for c in _children(window.grid))


def test_search_narrows_within_the_shelf(window):
    window.search.set_text("firefox")
    window._render()
    drain()
    assert [a["name"] for a in window.shown_apps()] == ["Firefox"]
    window.search.set_text("no such app anywhere")
    window._render()
    drain()
    assert window.stack.get_visible_child_name() == "empty"
    assert window.empty.get_title() == "Nothing matches that"


def test_an_empty_catalogue_says_who_approves_apps(monkeypatch):
    class Empty(DemoClient):
        def list_catalog(self):
            return {"apps": []}

    monkeypatch.setattr(store, "DaemonClient", Empty)
    win = store.Window()
    drain()
    assert win.stack.get_visible_child_name() == "empty"
    assert "No apps are approved yet" == win.empty.get_title()


def test_every_card_gets_an_icon_even_with_nothing_cached(window):
    for child in _children(window.grid):
        card = child.get_child()
        image = card.get_first_child().get_first_child()
        assert isinstance(image, Gtk.Image)
        assert image.get_icon_name() or image.get_paintable() or image.get_storage_type()


def test_installing_shows_progress_then_the_app_is_installed(window):
    card = next(c.get_child() for c in _children(window.grid)
                if c.get_child().ref == "org.gnome.Chess")
    assert card.button.get_label() == "Install"
    card.button.emit("clicked")
    assert card.state == "working"
    assert card.progress.get_visible() and not card.button.get_visible()
    for _ in range(40):          # the demo install ticks over about a second
        drain()
        import time

        time.sleep(0.05)
        if "org.gnome.Chess" in window.installed:
            break
    assert "org.gnome.Chess" in window.installed


def test_a_user_who_may_not_install_sees_why(monkeypatch):
    class NoInstalls(DemoClient):
        def get_policy(self):
            import os

            policy = super().get_policy()
            # The account at the keyboard, and only it: the demo family
            # already uses uid 1000, which is usually the developer's own.
            policy["users"] = [{"uid": os.getuid(), "username": "kid",
                                "mode": "filtered", "can_install_apps": False,
                                "admin": False}]
            return policy

    monkeypatch.setattr(store, "DaemonClient", NoInstalls)
    win = store.Window()
    drain()
    assert not win.can_install and not win.is_admin
    available = [c.get_child() for c in _children(win.grid)
                 if c.get_child().state == "available"]
    assert available
    for card in available:
        assert not card.button.get_sensitive()
        assert "turned off" in card.button.get_tooltip_text()
    installed = [c.get_child() for c in _children(win.grid)
                 if c.get_child().state == "installed"]
    for card in installed:   # removing is an admin's job
        assert not card.button.get_sensitive()
