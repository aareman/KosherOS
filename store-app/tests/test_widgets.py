"""The Store's shelves, its grid, and the icons on it, built for real.

Every UI bug this project has shipped was a widget that threw when it was
constructed, which a source-reading test cannot see. These build the real
window against the pretend daemon, on a virtual display.
"""

from __future__ import annotations

import pathlib

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


def type_search(window, text):
    """Type into the search box and let it act.

    GtkSearchEntry debounces search-changed by 150 ms, which no amount of
    main-loop draining makes pass, so the signal is emitted here — the same
    one a person's typing emits a moment later.
    """
    window.search.set_text(text)
    window.search.emit("search-changed")
    drain()


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


def test_it_opens_on_the_categories(window):
    # A store opens on its shelves, not on a flat list of everything.
    assert window.stack.get_visible_child_name() == "home"
    assert not window.back.get_visible()
    tiles = [c.key for c in _children(window.tiles)]
    # Updates first when there are any (the demo has two), Installed next,
    # everything last.
    assert tiles[:2] == ["updates", "installed"] and tiles[-1] == "all"
    assert "internet" in tiles and "games" in tiles
    assert str(len(window.catalog)) in window.home_subtitle.get_label()


def test_choosing_a_category_shows_it_beside_the_sidebar(window):
    games = next(c for c in _children(window.tiles) if c.key == "games")
    window.tiles.emit("child-activated", games)
    drain()
    assert window.stack.get_visible_child_name() == "browse"
    assert window.back.get_visible()
    assert window.shelf == "games"
    assert window.sidebar.get_selected_row().key == "games"
    shown = window.shown_apps()
    assert shown and all(store.shelf_of(a) == "games" for a in shown)
    assert len(_children(window.grid)) == len(shown)


def test_the_sidebar_lists_every_category_with_a_count(window):
    window.show_shelf("all")
    drain()
    rows = _children(window.sidebar)
    keys = [r.key for r in rows]
    assert keys[:3] == ["all", "updates", "installed"]
    assert "internet" in keys and "develop" in keys
    counts = window.shelf_counts()
    assert counts["all"] == len(window.catalog)
    assert counts["installed"] == len([a for a in window.catalog
                                       if a["ref"] in window.installed])


def test_the_sidebar_changes_what_is_shown(window):
    window.show_shelf("all")
    drain()
    row = next(r for r in _children(window.sidebar) if r.key == "music")
    window.sidebar.select_row(row)
    drain()
    assert window.shelf == "music"
    assert all(store.shelf_of(a) == "music" for a in window.shown_apps())


def test_searching_from_home_goes_to_the_results_with_the_sidebar(window):
    assert window.stack.get_visible_child_name() == "home"
    type_search(window, "firefox")
    assert window.stack.get_visible_child_name() == "browse"
    assert window.shelf == "all", "a search looks everywhere"
    assert [a["name"] for a in window.shown_apps()] == ["Firefox"]
    assert _children(window.sidebar), "the categories are still there, at the side"


def test_going_back_returns_to_the_categories_and_clears_the_search(window):
    type_search(window, "firefox")
    window.back.emit("clicked")
    drain()
    assert window.stack.get_visible_child_name() == "home"
    assert window.search.get_text() == ""
    assert window.shelf == "all"


def test_the_search_box_is_always_in_the_header(window):
    assert isinstance(window.search, Gtk.SearchEntry)
    assert window.search.get_parent() is not None
    assert window.search.get_visible()


def test_the_grid_has_a_card_per_app(window):
    window.show_shelf("all")
    drain()
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


def test_only_categories_with_apps_are_offered(window):
    keys = [k for k, _label in window.shelves_with_apps()]
    counts = window.shelf_counts()
    assert keys and all(counts[k] for k in keys)
    # Everything in the shipped catalogue is classified, so that shelf is absent.
    assert "other" not in keys


def test_the_installed_category_shows_what_is_installed(window):
    window.show_shelf("installed")
    drain()
    shown = window.shown_apps()
    assert {a["ref"] for a in shown} <= window.installed
    # An installed app's button removes it — unless a newer build is
    # waiting, in which case the one button on the card is the update.
    assert all(c.get_child().button.get_label() in ("Remove", "Update")
               for c in _children(window.grid))
    assert any(c.get_child().button.get_label() == "Remove" for c in _children(window.grid))


def test_a_search_with_no_matches_says_so(window):
    type_search(window, "no such app anywhere")
    assert window.results.get_visible_child_name() == "empty"
    assert window.empty.get_title() == "Nothing matches that"


def test_a_search_can_be_narrowed_to_one_category(window):
    window.show_shelf("internet")
    type_search(window, "fire")
    assert window.shelf == "internet"
    assert [a["name"] for a in window.shown_apps()] == ["Firefox"]


def test_an_empty_catalogue_says_who_approves_apps(monkeypatch):
    class Empty(DemoClient):
        def list_catalog(self):
            return {"apps": []}

    monkeypatch.setattr(store, "DaemonClient", Empty)
    win = store.Window()
    drain()
    win.show_shelf("all")
    drain()
    assert win.results.get_visible_child_name() == "empty"
    assert "No apps are approved yet" == win.empty.get_title()
    assert "KosherOS Admin" in win.home_subtitle.get_label()


def test_every_card_gets_an_icon_even_with_nothing_cached(window):
    # A cached file or a themed icon where the machine has one; otherwise
    # the app's initial on a tile (letter_tile). On a bare virtual display
    # with no icon theme to speak of, most cards take the tile — which is
    # the case this test is about: never an empty space.
    window.show_shelf("all")
    drain()
    for child in _children(window.grid):
        card = child.get_child()
        image = card.get_first_child().get_first_child()
        if isinstance(image, Gtk.Label):
            assert image.get_label().strip() and image.has_css_class("app-letter")
        else:
            assert isinstance(image, Gtk.Image)
            assert image.get_icon_name() or image.get_paintable() or image.get_storage_type()


def test_installing_shows_progress_then_the_app_is_installed(window):
    window.show_shelf("all")
    drain()
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
    win.show_shelf("all")
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


# -- updates ----------------------------------------------------------------------------

def test_the_store_offers_updates_one_by_one_and_all_at_once(window):
    # "app store should have an updater section (update individually,
    # update all, etc.)". The pretend daemon says two installed apps have a
    # newer build.
    assert len(window.updates) == 2
    tiles = [c.get_child() for c in _children(window.tiles)]
    assert tiles[0].key == "updates" if hasattr(tiles[0], "key") else True
    home_keys = [c.key for c in _children(window.tiles)]
    assert home_keys[0] == "updates", "an update is the first thing a store says"
    window.show_shelf("updates")
    drain()
    cards = list(window.cards.values())
    assert len(cards) == 2
    assert all(c.state == "update" and c.button.get_label() == "Update" for c in cards)
    assert "Version 2.1" in cards[0].button.get_tooltip_text()
    assert window.update_all.get_visible()
    # One app on its own …
    cards[0]._on_clicked(None)
    assert cards[0].state == "working"
    for _ in range(12):
        drain()
        import time; time.sleep(0.15)
    # … the demo finishes it, the store reloads, and one update is left.
    assert len(window.updates) == 1
    # … and the rest all at once.
    window.show_shelf("updates")
    drain()
    window._update_all()
    for _ in range(12):
        drain()
        import time; time.sleep(0.15)
    assert window.updates == {}
    assert window.empty.get_title() == "Everything is up to date"
    assert not window.update_all.get_visible()


def test_an_app_with_no_update_keeps_its_ordinary_button(window):
    window.show_shelf("installed")
    drain()
    states = {ref: card.state for ref, card in window.cards.items()}
    assert set(states.values()) == {"update", "installed"}
    assert sum(1 for s in states.values() if s == "update") == 2


# -- what is in hand, and what is installed ---------------------------------------

def test_an_install_in_hand_survives_a_rebuild_and_cannot_be_asked_for_twice(window):
    asked = []

    class Counting(type(window.client)):
        pass

    window.client.install_app = lambda ref: asked.append(ref)
    window.show_shelf("all")
    drain()
    card = next(c.get_child() for c in _children(window.grid)
                if c.get_child().ref == "org.gnome.Chess")
    card.button.emit("clicked")
    drain()
    assert asked == ["org.gnome.Chess"]
    assert window.working["org.gnome.Chess"][1] == "Queued…"
    # A second press does nothing: the button is hidden, and the handler
    # refuses anyway.
    card._on_clicked(None)
    assert asked == ["org.gnome.Chess"]
    # Every later render still shows it as working — this is what used to
    # be lost, leaving an Install button that asked for it again.
    window.show_shelf("all")
    drain()
    rebuilt = next(c.get_child() for c in _children(window.grid)
                   if c.get_child().ref == "org.gnome.Chess")
    assert rebuilt is not card
    assert rebuilt.state == "working"
    assert rebuilt.progress.get_visible() and not rebuilt.button.get_visible()


def test_the_installing_shelf_lists_what_is_being_installed(window):
    assert "installing" not in [k for k, _l in _sidebar_rows(window)]
    window._on_progress("org.gnome.Chess", 40, "Downloading")
    drain()
    assert window.shelf_counts()["installing"] == 1
    assert ("installing", "Installing") in _sidebar_rows(window)
    window.show_shelf("installing")
    drain()
    shown = window.shown_apps()
    assert [a["ref"] for a in shown] == ["org.gnome.Chess"]
    card = _children(window.grid)[0].get_child()
    assert card.state == "working"
    assert "Downloading" in card.progress.get_text()
    # When it finishes the shelf empties again.
    window._on_finished("org.gnome.Chess", True, "")
    drain()
    assert window.working == {}
    assert ("installing", "Installing") not in _sidebar_rows(window)


def test_the_installing_shelf_says_so_when_nothing_is_happening(window):
    window.show_shelf("installing")
    drain()
    assert window.results.get_visible_child_name() == "empty"
    assert window.empty.get_title() == "Nothing is being installed"


def test_installed_lists_what_the_machine_has_even_outside_the_catalogue(monkeypatch):
    # An app approved when it was installed, since taken off the list, is
    # still on the machine. The shelf used to filter the catalogue, so it
    # showed nothing for it.
    class Extra(DemoClient):
        def list_installed_details(self):
            return super().list_installed_details() + [
                {"ref": "org.example.Old", "name": "An Older App", "approved": False}]

        def list_installed(self):
            return super().list_installed() + ["org.example.Old"]

    monkeypatch.setattr(store, "DaemonClient", Extra)
    win = store.Window()
    drain()
    win.show_shelf("installed")
    drain()
    refs = [a["ref"] for a in win.shown_apps()]
    assert "org.example.Old" in refs
    assert win.shelf_counts()["installed"] == len(refs)
    entry = next(a for a in win.shown_apps() if a["ref"] == "org.example.Old")
    assert entry["name"] == "An Older App"
    assert "no longer on the approved list" in entry["summary"]


def test_installed_says_so_when_nothing_is_installed(monkeypatch):
    class Nothing(DemoClient):
        def list_installed(self):
            return []

        def list_installed_details(self):
            return []

    monkeypatch.setattr(store, "DaemonClient", Nothing)
    win = store.Window()
    drain()
    win.show_shelf("installed")
    drain()
    assert win.results.get_visible_child_name() == "empty"
    assert win.empty.get_title() == "Nothing is installed yet"


def test_the_grid_has_a_fixed_number_of_columns_at_any_one_width(window):
    # "app lists flicker between two column and one": left to choose for
    # itself, the grid and the scrollbar chased each other.
    assert window.grid.get_min_children_per_line() == window.grid.get_max_children_per_line() == 2
    source = (pathlib.Path(store.__file__)).read_text()
    assert 'Adw.BreakpointCondition.parse("max-width: 880sp")' in source
    assert 'narrow.add_setter(self.grid, "min-children-per-line", 1)' in source
    # And a card cannot change height as it changes width.
    window.show_shelf("all")
    drain()
    card = _children(window.grid)[0].get_child()
    width, height = card.get_size_request()
    assert width > 0 and height > 0


def _sidebar_rows(window):
    rows = []
    row = window.sidebar.get_first_child()
    while row is not None:
        key = getattr(row, "key", None)
        label = row.get_child().get_first_child().get_next_sibling()
        rows.append((key, label.get_label()))
        row = row.get_next_sibling()
    return rows


def test_updates_are_always_findable_even_with_nothing_to_update(monkeypatch):
    # "where is the feature to update apps": the shelf used to appear only
    # when something needed updating, which on a machine that had never
    # fetched the remote's news was never.
    class UpToDate(DemoClient):
        def list_app_updates(self):
            return []

    monkeypatch.setattr(store, "DaemonClient", UpToDate)
    win = store.Window()
    drain()
    assert ("updates", "Updates") in _sidebar_rows(win)
    tiles = [c.get_child() for c in _children(win.tiles)]
    updates_tile = next(t for t in tiles if t.key == "updates")
    words = [w.get_label() for w in _children(updates_tile) if isinstance(w, Gtk.Label)]
    assert "Up to date" in words
    win.show_shelf("updates")
    drain()
    assert win.check_button.get_visible(), "and a way to go and look"
    assert not win.update_all.get_visible()


def test_checking_for_updates_asks_the_remote_and_says_what_it_found(window):
    asked = []
    window.client.check_app_updates = lambda: (
        asked.append(1) or [{"ref": "org.gnome.Chess", "name": "Chess", "version": "4.2"}])
    window.show_shelf("updates")
    drain()
    window.check_button.emit("clicked")
    drain()
    assert asked, "the button goes to the daemon, which goes to the remote"
    assert window.check_button.get_label() == "Check for updates"
    assert "org.gnome.Chess" in window.updates
    assert window.state_of("org.gnome.Chess") == "update"
    assert [a["ref"] for a in window.shown_apps()] == ["org.gnome.Chess"]

