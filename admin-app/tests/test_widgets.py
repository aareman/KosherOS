"""Building the admin app's widgets for real.

Every UI bug this project has shipped was a widget that threw when it was
constructed — a GLib.Variant unpacked the wrong way, a polkit action
prompting twice. A test that parses the source cannot see any of that, so
these build the actual pages and dialogs against a stub client, on a
virtual display.

Skipped, not failed, where GTK is unavailable: the point is to catch the
bug on a developer's machine, not to make the suite unrunnable elsewhere.
"""

from __future__ import annotations

import time

import pytest

gi = pytest.importorskip("gi")
try:
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Adw, Gtk
except ValueError:  # pragma: no cover - no typelibs on this machine
    pytest.skip("GTK4/libadwaita not available", allow_module_level=True)

if not Gtk.init_check():  # pragma: no cover - no display
    pytest.skip("no display", allow_module_level=True)
Adw.init()

from kosheradmin import app as admin  # noqa: E402
from kosheradmin import detail, dialogs, family, feed, labels  # noqa: E402


class FakeClient:
    """Answers the calls a page makes while it is being built."""

    def __init__(self, **overrides):
        self.calls = []
        self._overrides = overrides

    def get_list_edits(self, name):
        self.calls.append(("get_list_edits", name))
        return self._overrides.get("edits",
                                   {"add": {}, "remove": [], "shipped": 119})

    def list_categories(self):
        return {"domains": 5_000_000, "version": "1", "source": "test",
                "categories": [{"name": "adult", "label": "Adult"},
                               {"name": "gambling", "label": "Gambling"},
                               {"name": "sports", "label": "Sports"},
                               {"name": "ads", "label": "Advertising"}]}

    def list_installed_details(self):
        return self._overrides.get("installed", [])

    def filter_status(self):
        return self._overrides.get("status", {"pictures": "checking",
                                              "degraded": [], "problems": [],
                                              "services": {"kosher-mitm.service": "active"}})

    def list_requests(self):
        return self._overrides.get("requests", [])

    def list_activity(self, since=0, uid=-1):
        found = self._overrides.get("activity", [])
        return [e for e in found if uid < 0 or e["uid"] == uid]

    def activity_summary(self):
        return self._overrides.get("summary", {})

    def list_catalog(self):
        return {"apps": self._overrides.get("catalog", [])}

    def list_installed(self):
        return []

    def deployment_status(self):
        return self._overrides.get("deployment", {
            "booted": {"image": "ghcr.io/x/kosheros:stable", "version": "2026.09.10",
                       "timestamp": 1},
            "rollback": {"image": "ghcr.io/x/kosheros:stable", "version": "2026.09.03",
                         "timestamp": 0},
            "staged": None, "rollback_queued": False})

    def check_update(self):
        return "Up to date"


class FakeWindow(Gtk.Window):
    """Enough of Window for a page to be built, driven and presented.

    A real Gtk.Window, not a stand-in: dialogs are presented into their
    parent, and a stub cannot be a parent.
    """

    def __init__(self, client=None, **state):
        super().__init__()
        self.client = client or FakeClient()
        self.toasts = []
        self.policy = {"revision": 1, "users": [], "guardian": {"enabled": False},
                       "guest": {"enabled": False}}
        self.requests = state.get("requests", [])
        self.status = state.get("status", self.client.filter_status())
        self.summary = state.get("summary", {})
        self.catalog_count = state.get("catalog_count")
        self.update_state = None
        self.pushed = []
        self.shown_activity = []

    def toast(self, text):
        self.toasts.append(text)

    def call(self, work, refresh=True, done_msg=None, on_done=None, after_reload=None):
        work()
        if on_done:
            on_done()
        if after_reload:
            after_reload()

    def with_guardian(self, then):
        then("")

    def reload(self):
        pass

    def push(self, page):
        self.pushed.append(page)

    def open_user_detail(self, user):
        self.pushed.append(("detail", user["uid"]))

    def show_activity(self, uid):
        self.shown_activity.append(uid)


def drain():
    """Let the async loads finish and their idle callbacks run."""
    from gi.repository import GLib

    context = GLib.MainContext.default()
    for _ in range(200):
        while context.pending():
            context.iteration(False)


def a_user(**overrides):
    user = {"uid": 1001, "username": "yosef", "mode": "filtered", "admin": False,
            "whitelist": [], "rules": [], "apps": [],
            "blocked_categories": ["adult", "gambling", "dating", "malware",
                                   "proxy", "social", "video", "immodest",
                                   "violence", "drugs"],
            "media_level": "immodest", "language_filter": "substitute",
            "youtube": {"restrict": "strict", "blocked_categories": ["24", "20", "10"]},
            "can_install_apps": False}
    user.update(overrides)
    return user


def _rows(widget, found=None):
    found = [] if found is None else found
    child = widget.get_first_child()
    while child is not None:
        if isinstance(child, Adw.PreferencesRow):
            found.append(child)
        _rows(child, found)
        child = child.get_next_sibling()
    return found


def _row_named(widget, title):
    for row in _rows(widget):
        if row.get_title() == title:
            return row
    raise AssertionError(f"no row titled {title!r}")


def _texts(widget, found=None):
    found = [] if found is None else found
    child = widget.get_first_child()
    while child is not None:
        if isinstance(child, Gtk.Label):
            found.append(child.get_label())
        _texts(child, found)
        child = child.get_next_sibling()
    return found


# -- the list editors -----------------------------------------------------------

@pytest.mark.parametrize("name,title,description,noun", labels.EDITABLE_LISTS,
                         ids=[e[0] for e in labels.EDITABLE_LISTS])
def test_every_list_dialog_builds(name, title, description, noun):
    win = FakeWindow()
    dialog = dialogs.ListEditDialog(win, name, title, description, noun)
    drain()
    assert dialog.shipped == 119


def test_the_summary_says_how_many_ship_and_how_many_you_changed():
    win = FakeWindow(FakeClient(edits={"add": {"blast": "bother"},
                                       "remove": ["heck"], "shipped": 119}))
    dialog = dialogs.ListEditDialog(win, "wordlist.json", "Bad language", "", "word")
    drain()
    assert dialog.summary.get_title() == "119 come with KosherOS · you added 1 · you removed 1"


def test_saving_a_list_sends_exactly_what_is_shown():
    sent = {}

    class Recording(FakeClient):
        def edit_list(self, name, add, remove, pw):
            sent.update(name=name, add=add, remove=remove)

    win = FakeWindow(Recording(edits={"add": {"blast": "bother"}, "remove": [],
                                      "shipped": 5}))
    dialog = dialogs.ListEditDialog(win, "wordlist.json", "Bad language", "", "word")
    drain()
    dialog._drop_added("blast")
    dialog._save()
    assert sent == {"name": "wordlist.json", "add": {}, "remove": []}


def test_the_whitelist_dialog_builds_and_filters():
    win = FakeWindow()
    domains = [f"site{i}.example" for i in range(500)] + ["chinuch.org"]
    dialog = dialogs.WhitelistDialog(win, "Whitelist", domains, lambda new: None)
    drain()
    dialog.search.set_text("chinuch")
    dialog._rebuild()
    drain()
    rows = []
    child = dialog.list_box.get_first_child()
    while child is not None:
        rows.append(child)
        child = child.get_next_sibling()
    assert len(rows) == 1


def test_the_rules_dialog_builds_with_rules():
    win = FakeWindow()
    rules = [{"action": "block", "pattern": "youtube.com/shorts*"},
             {"action": "allow", "pattern": "chinuch.org"}]
    dialog = dialogs.RulesDialog(win, "Page rules", rules, lambda new: None)
    drain()
    assert len(dialog.rules) == 2


def test_the_save_preset_dialog_needs_a_name():
    win = FakeWindow()
    dialog = dialogs.SavePresetDialog(win, a_user())
    assert not dialog.save_button.get_sensitive()
    dialog.name.set_text("Mine")
    assert dialog.save_button.get_sensitive()


# -- the family board -----------------------------------------------------------

def a_board(users=None, **state):
    client_overrides = {k: state.pop(k) for k in ("status", "requests", "activity",
                                                  "summary", "installed", "catalog")
                        if k in state}
    win = FakeWindow(FakeClient(**client_overrides), **state)
    win.policy = {"revision": 1, "users": users if users is not None else [a_user()],
                  "guardian": {"enabled": False}, "adblock": {"enabled": True},
                  "guest": {"enabled": False, "mode": "whitelist", "whitelist": []}}
    win.requests = client_overrides.get("requests", [])
    win.status = client_overrides.get("status", win.client.filter_status())
    win.summary = client_overrides.get("summary", {})
    page = family.FamilyPage(win)
    page.refresh()
    drain()
    return win, page


def _children(flowbox):
    found = []
    child = flowbox.get_first_child()
    while child is not None:
        found.append(child)
        child = child.get_next_sibling()
    return found


def test_the_board_has_a_card_per_person_and_one_for_the_guest():
    from kosherd import profiles

    users = []
    for i, profile in enumerate(profiles.PROFILES):
        users.append(a_user(uid=1001 + i, username=f"u{i}", mode=profile.mode,
                            blocked_categories=list(profile.blocked_categories),
                            media_level=profile.media_level,
                            language_filter=profile.language_filter,
                            youtube=dict(profile.youtube),
                            can_install_apps=profile.can_install_apps))
    win, page = a_board(users)
    assert len(_children(page.cards)) == len(users) + 1


def test_a_card_has_the_four_lines_in_a_fixed_order():
    win, page = a_board([a_user()])
    card = _children(page.cards)[0]
    texts = _texts(card)
    web = next(i for i, t in enumerate(texts) if t.startswith("Filtered internet"))
    pictures = next(i for i, t in enumerate(texts) if "pictures" in t.lower())
    video = next(i for i, t in enumerate(texts) if t.startswith("YouTube"))
    apps = next(i for i, t in enumerate(texts) if "app" in t.lower())
    assert web < pictures < video < apps


def test_a_card_names_the_preset_and_its_drift():
    user = a_user()
    win, page = a_board([user])
    assert any("Child" in t and "change" not in t for t in _texts(_children(page.cards)[0]))
    drifted = a_user(blocked_categories=[*user["blocked_categories"], "sports"])
    win, page = a_board([drifted])
    assert any(t == "Child, with 1 change" for t in _texts(_children(page.cards)[0]))


def test_a_card_says_what_happened_today():
    win, page = a_board([a_user()], summary={"1001": {"blocked": 14, "pictures": 2,
                                                       "searches": 0, "last": 1}})
    texts = _texts(_children(page.cards)[0])
    assert any(t.startswith("14 blocked, pictures hidden on 2 pages today") for t in texts)
    win, page = a_board([a_user()], summary={})
    assert "Nothing blocked today" in _texts(_children(page.cards)[0])


def test_clicking_a_card_opens_the_persons_page_and_the_guest_opens_its_own():
    win, page = a_board([a_user()])
    cards = _children(page.cards)
    page._on_card(page.cards, cards[0])
    assert win.pushed == [("detail", 1001)]
    page._on_card(page.cards, cards[1])
    assert isinstance(win.pushed[1], family.GuestPage)


def test_waiting_requests_are_a_banner_not_a_list():
    # The user's words: "a blue notification at top: X requests waiting for
    # you, click to deal with".
    win, page = a_board([a_user()], requests=[])
    assert not page.requests_banner.get_revealed()

    win, page = a_board([a_user()], requests=[
        {"id": "a" * 32, "uid": 1001, "username": "yosef",
         "url": "https://example.com/needed", "note": "for school",
         "mode": "filtered", "asked": 0, "why": "category:video"},
        {"id": "b" * 32, "uid": 1001, "username": "yosef",
         "url": "https://other.example/", "note": "", "mode": "filtered", "asked": 0}])
    assert page.requests_banner.get_revealed()
    assert page.requests_banner.get_title() == "2 requests waiting for you"
    assert page.requests_banner.has_css_class("requests-banner")
    # ...and the card carries the count too.
    assert "2 requests" in _texts(_children(page.cards)[0])


def test_the_requests_dialog_shows_why_and_answers_inline():
    granted = {}

    class Recording(FakeClient):
        def approve_request(self, request_id, whole_site, pw):
            granted.update(id=request_id, whole_site=whole_site)

    win = FakeWindow(Recording())
    request = {"id": "a" * 32, "uid": 1001, "username": "yosef",
               "url": "https://youtube.com/watch?v=x", "note": "for school",
               "mode": "filtered", "asked": int(time.time()) - 1200,
               "why": "category:video"}
    dialog = dialogs.RequestsDialog(win, [request])
    row = dialog.rows[0]
    assert "yosef asked for youtube.com, 20 min ago" == row.get_title()
    assert "blocked by Video and streaming" in row.get_subtitle()
    assert "“for school”" in row.get_subtitle()
    buttons = [w for w in _buttons(row)]
    labels_ = [b.get_label() for b in buttons]
    assert labels_ == ["Just this page", "All of youtube.com", "No"]
    buttons[1].emit("clicked")
    drain()
    assert granted == {"id": "a" * 32, "whole_site": True}
    assert dialog.requests == []


def _buttons(widget, found=None):
    found = [] if found is None else found
    child = widget.get_first_child()
    while child is not None:
        if isinstance(child, Gtk.Button):
            found.append(child)
        _buttons(child, found)
        child = child.get_next_sibling()
    return found


def test_a_whitelist_request_offers_only_the_whole_site():
    win = FakeWindow()
    request = {"id": "b" * 32, "uid": 1001, "username": "shmuli", "mode": "whitelist",
               "url": "https://chinuch.org/x", "asked": 0}
    row = dialogs.request_row(win, request)
    assert [b.get_label() for b in _buttons(row)] == ["Allow chinuch.org", "No"]


def test_health_speaks_when_it_is_fine_too():
    win, page = a_board([a_user()])
    assert not page.health_banner.get_revealed()
    assert page.health_tag.get_visible()
    assert page.health_tag.get_label().startswith("Filter running")


def test_a_problem_is_an_amber_banner_with_the_details_behind_it():
    win, page = a_board([a_user()], status={"pictures": "no_model", "detect_ms": None,
                                            "degraded": ["kosher-dns.service"],
                                            "problems": [], "services": {}})
    assert page.health_banner.get_revealed()
    assert page.health_banner.get_title() == "Pictures are being hidden, not checked"
    rows = family.health_rows(win.status)
    assert [ok for _t, _b, ok in rows] == [False, False]


def test_every_reportable_problem_produces_a_row():
    rows = family.health_rows({"pictures": "too_slow", "detect_ms": 620,
                               "degraded": ["kosher-mitm.service"],
                               "problems": ["the bad-language list did not load"],
                               "services": {"kosher-search.service": "active"}})
    titles = [t for t, _b, _ok in rows]
    assert titles[:3] == ["This computer is hiding pictures instead of checking them",
                          "Part of the filter is not running",
                          "A filter list is missing or damaged"]
    assert "620" in rows[0][1]


def test_the_computer_tiles_open_their_pages():
    win, page = a_board([a_user()], catalog_count=23)
    tiles = _children(page.tiles)
    assert [t.key for t in tiles] == ["apps", "lists", "ads", "guardian", "updates"]
    assert "23 apps approved" in _texts(tiles[0])
    assert "Blocked for everyone" in _texts(tiles[2])
    for tile in (tiles[1], tiles[2], tiles[4]):
        page._on_tile(page.tiles, tile)
    drain()
    assert [type(p).__name__ for p in win.pushed] == ["WordListsPage", "AdsPage",
                                                       "UpdatesPage"]


def test_switching_ad_blocking_off_goes_through_the_guardian():
    asked = []

    class Recording(FakeClient):
        def set_adblock(self, enabled, pw=""):
            asked.append(enabled)

    win = FakeWindow(Recording())
    win.policy = {"revision": 1, "users": [], "guardian": {"enabled": False},
                  "adblock": {"enabled": True}, "guest": {"enabled": False}}
    page = family.AdsPage(win)
    page.row.set_active(False)
    drain()
    assert asked == [False]


def test_the_guest_is_set_up_by_kind_of_internet_not_preset():
    # Nobody knows who the guest is, so "Child" or "Teenager" is the wrong
    # question; the kind of internet is the right one, and each kind still
    # carries sensible content settings through the preset it maps to.
    sent = {}

    class Recording(FakeClient):
        def set_guest_config(self, enabled, mode, whitelist, pw):
            sent.update(enabled=enabled, mode=mode)

    win = FakeWindow(Recording())
    win.policy = {"revision": 1, "users": [], "guardian": {"enabled": False},
                  "guest": {"enabled": True, "mode": "whitelist", "whitelist": []}}
    page = family.GuestPage(win)
    combo = _row_named(page, "Start the guest off with")
    options = [combo.get_model().get_string(i) for i in range(combo.get_model().get_n_items())]
    assert options == [labels.MODE_LABELS[m] for m in ("none", "whitelist", "dnsfilter",
                                                        "filtered", "unfiltered")]
    assert options[combo.get_selected()] == "Whitelist only"
    combo.set_selected(options.index("Filtered internet"))
    drain()
    assert sent == {"enabled": True, "mode": "child"}


def test_an_enabled_guest_opens_the_same_page_as_anyone_without_an_apps_tab():
    win = FakeWindow(FakeClient())
    win.policy = {"revision": 1, "users": [], "guardian": {"enabled": False},
                  "adblock": {"enabled": True},
                  "guest": {"enabled": True, "uid": 1010, "mode": "filtered",
                            "blocked_categories": ["adult"], "media_level": "immodest"}}
    guest = family.guest_user(win.policy)
    assert guest["username"] == "Guest" and guest["guest"] is True
    page = detail.UserDetailPage(win, guest)
    drain()
    names = [page.stack.get_pages().get_item(i).get_name()
             for i in range(page.stack.get_pages().get_n_items())]
    assert names == ["overview", "filtering", "media", "youtube", "account"]
    titles = {r.get_title() for r in _rows(page)}
    assert "Guest account is on" in titles
    assert "Administrator" not in titles and "Remove This Account…" not in titles
    # Every filter setting is there to change.
    for title in ("Filter mode", "Pictures and video", "Bad language", "Restricted Mode",
                  "Page rules"):
        assert title in titles, title


def test_the_guest_card_routes_to_turn_on_when_off_and_to_the_page_when_on():
    win, page = a_board([])
    cards = _children(page.cards)
    page._on_card(page.cards, cards[-1])
    assert isinstance(win.pushed[-1], family.GuestPage)
    win.policy["guest"] = {"enabled": True, "uid": 1010, "mode": "whitelist"}
    page._on_card(page.cards, cards[-1])
    assert win.pushed[-1] == ("detail", 1010)
    assert family.guest_user({"guest": {"enabled": False}}) is None


def test_the_guest_card_fills_its_cell_like_the_others():
    card = family.guest_card({"enabled": False})
    assert card.get_valign() == Gtk.Align.FILL
    assert card.get_halign() == Gtk.Align.FILL
    person = family.person_card(a_user(), None, 0)
    assert card.get_size_request()[0] == person.get_size_request()[0]


def test_every_card_on_the_board_shares_one_height():
    win, page = a_board([a_user(), a_user(uid=1002, username="rivky")])
    cards = [c.get_child() for c in _children(page.cards)]
    grouped = list(page.card_heights.get_widgets())
    assert len(grouped) == len(cards) == 3
    assert page.card_heights.get_mode() == Gtk.SizeGroupMode.VERTICAL
    heights = {c.measure(Gtk.Orientation.VERTICAL, 260)[0] for c in cards}
    assert len(heights) == 1, heights


def test_a_username_is_suggested_from_the_full_name_and_checked():
    assert dialogs.suggest_username("Elisha Ben-David") == "elisha"
    assert dialogs.suggest_username("  Chaya   Sara ") == "chaya"
    assert dialogs.suggest_username("Élodie") == "elodie"
    assert dialogs.suggest_username("123") == "u123"
    assert dialogs.suggest_username("") == ""
    assert dialogs.username_ok("Elisha") and dialogs.username_ok("elisha.b")
    assert not dialogs.username_ok("eli sha") and not dialogs.username_ok("9lives")
    assert not dialogs.username_ok("")


# -- the activity feed -----------------------------------------------------------

def an_event(**overrides):
    event = {"t": int(time.time()) - 60, "kind": "block", "uid": 1001,
             "username": "yosef", "url": "https://roblox.com/games",
             "why": "category:games"}
    event.update(overrides)
    return event


def a_feed(events, users=None, requests=()):
    win = FakeWindow(FakeClient(activity=events, requests=list(requests)))
    win.policy = {"revision": 1, "users": users if users is not None else [a_user()],
                  "guardian": {"enabled": False}, "guest": {"enabled": False}}
    page = feed.ActivityPage(win)
    page.refresh()
    drain()
    return win, page


def test_the_feed_lists_people_as_filters_with_everyone_first():
    win, page = a_feed([], users=[a_user(), a_user(uid=1002, username="rivky")])
    rows = _children(page.people)
    assert [r.uid for r in rows] == [-1, 1001, 1002]
    assert page.people.get_selected_row() is rows[0]


def test_a_quiet_day_says_so_rather_than_showing_nothing():
    win, page = a_feed([])
    assert "Nothing to show" in _texts(page.feed)


def test_a_block_reads_in_the_familys_words_and_can_be_allowed():
    win, page = a_feed([an_event()])
    rows = _rows(page.feed)
    assert rows[0].get_title() == "Blocked roblox.com/games"
    assert "Games" in rows[0].get_subtitle()
    assert any(isinstance(b, Gtk.MenuButton) for b in _menu_buttons(rows[0]))


def _menu_buttons(widget, found=None):
    found = [] if found is None else found
    child = widget.get_first_child()
    while child is not None:
        if isinstance(child, Gtk.MenuButton):
            found.append(child)
        _menu_buttons(child, found)
        child = child.get_next_sibling()
    return found


def test_a_block_by_an_admins_own_rule_is_not_offered_an_allow():
    win, page = a_feed([an_event(why="rule:youtube.com/shorts*")])
    assert not _menu_buttons(_rows(page.feed)[0])
    assert "a page rule" in _rows(page.feed)[0].get_subtitle()


def test_repeats_fold_into_one_line():
    now = int(time.time())
    events = [an_event(t=now - i * 10) for i in range(5)]
    folded = feed.fold(events)
    assert len(folded) == 1 and folded[0]["times"] == 5
    win, page = a_feed(events)
    assert "5 times" in _rows(page.feed)[0].get_subtitle()


def test_every_kind_of_event_renders():
    now = int(time.time())
    events = [an_event(t=now - 1),
              an_event(t=now - 2, kind="pictures", url="https://shop.example/dress", why=""),
              an_event(t=now - 3, kind="video", url="https://tube.example/v", why=""),
              an_event(t=now - 4, kind="search", url="", text="bad words", why="a blocked search"),
              an_event(t=now - 5, kind="change", url="", why="", by=1000,
                       by_username="avi", method="SetFilterMode",
                       args=[1001, "dnsfilter"], guardian=True)]
    win, page = a_feed(events)
    titles = [r.get_title() for r in _rows(page.feed)]
    assert titles == ["Blocked roblox.com/games",
                      "Hid pictures on shop.example/dress",
                      "Refused a video on tube.example/v",
                      "Would not search for “bad words”",
                      "yosef: filter mode → DNS filter"]
    change = _rows(page.feed)[-1]
    assert change.get_subtitle().startswith("avi, ")
    assert "guardian password given" in change.get_subtitle()


def test_narrowing_to_one_person_asks_the_daemon_for_them():
    asked = []

    class Recording(FakeClient):
        def list_activity(self, since=0, uid=-1):
            asked.append(uid)
            return []

    win = FakeWindow(Recording())
    win.policy = {"revision": 1, "users": [a_user()], "guardian": {"enabled": False},
                  "guest": {"enabled": False}}
    page = feed.ActivityPage(win)
    page.show_user(1001)
    drain()
    assert asked[-1] == 1001
    assert page.people.get_selected_row().uid == 1001


def test_yesterday_and_today_get_their_own_headings():
    now = int(time.time())
    win, page = a_feed([an_event(t=now - 30), an_event(t=now - 86400 - 30)])
    headings = [t for t in _texts(page.feed) if t in ("Today", "Yesterday")]
    assert headings == ["Today", "Yesterday"]


# -- one person's page --------------------------------------------------------------

def _detail(mode="filtered", users=None, **kw):
    activity = kw.pop("activity", [])
    requests = kw.pop("requests", [])
    installed = kw.pop("installed", [])
    custom = kw.pop("custom_profiles", [])
    win = FakeWindow(FakeClient(activity=activity, requests=requests,
                                installed=installed))
    win.requests = requests
    user = a_user(mode=mode, **kw)
    win.policy = {"revision": 1, "users": users or [user], "custom_profiles": custom,
                  "guardian": {"enabled": False}, "guest": {"enabled": False},
                  "adblock": {"enabled": True}}
    page = detail.UserDetailPage(win, user)
    drain()
    return page, win, user


def test_the_detail_page_has_six_tabs_and_starts_on_the_overview():
    page, _w, _u = _detail()
    names = []
    stack_pages = page.stack.get_pages()
    for i in range(stack_pages.get_n_items()):
        names.append(stack_pages.get_item(i).get_name())
    assert names == ["overview", "filtering", "media", "youtube", "apps", "account"]
    assert page.stack.get_visible_child_name() == "overview"


def test_every_setting_made_the_move():
    page, _w, _u = _detail()
    titles = {r.get_title() for r in _rows(page)}
    for title in ("Set up as", "Filter mode", "Approved sites", "Page rules",
                  "Pictures and video", "Bad language", "Restricted Mode",
                  "Can install approved apps", "Allow Wi-Fi sign-in",
                  "Administrator", "Layout", "Remove This Account…"):
        assert title in titles, f"{title} was lost in the move"


def test_drift_is_named_and_there_is_a_way_back():
    applied = {}

    class Recording(FakeClient):
        def apply_profile(self, uid, key, pw):
            applied.update(uid=uid, key=key)

    win = FakeWindow(Recording())
    user = a_user(blocked_categories=[*a_user()["blocked_categories"], "sports"],
                  youtube={"restrict": "strict",
                           "blocked_categories": ["24", "20", "10", "17"]})
    win.policy = {"revision": 1, "users": [user], "guardian": {"enabled": False},
                  "guest": {"enabled": False}, "adblock": {"enabled": True}}
    page = detail.UserDetailPage(win, user)
    drain()
    texts = _texts(page)
    assert "Set up as Child, with 2 changes" in texts
    assert "Sports also blocked" in texts
    assert any(t.startswith("YouTube: ") and "also blocked" in t for t in texts)
    reset = next(b for b in _buttons(page) if b.get_label() == "Reset to Child")
    reset.emit("clicked")
    drain()
    assert applied == {"uid": 1001, "key": "child"}


def test_an_account_on_its_preset_has_no_reset_button():
    page, _w, _u = _detail()
    assert not any(b.get_label() and b.get_label().startswith("Reset to")
                   for b in _buttons(page))
    assert "Set up as Child" in _texts(page)


def test_the_protection_strip_has_the_four_lines():
    page, _w, _u = _detail()
    texts = _texts(page)
    for expected in ("Filtered internet · 10 kinds of site blocked",
                     "Immodest pictures hidden", "YouTube strict, 3 kinds blocked",
                     "All apps"):
        assert expected in texts, expected


def test_the_overview_shows_what_was_blocked_today_with_allow():
    allowed = {}

    class Recording(FakeClient):
        def allow_url(self, uid, url, whole, pw):
            allowed.update(uid=uid, url=url, whole=whole)

    now = int(time.time())
    events = [an_event(t=now - 5), an_event(t=now - 8, kind="pictures",
                                            url="https://shop.example/d", why="")]
    win = FakeWindow(Recording(activity=events))
    user = a_user()
    win.policy = {"revision": 1, "users": [user], "guardian": {"enabled": False},
                  "guest": {"enabled": False}, "adblock": {"enabled": True}}
    page = detail.UserDetailPage(win, user)
    drain()
    titles = [r.get_title() for r in _rows(page.blocked_group)]
    assert titles == ["roblox.com/games", "Pictures hidden on shop.example/d"]
    menus = _menu_buttons(page.blocked_group)
    assert len(menus) == 1
    page_btn = next(b for b in _buttons(menus[0].get_popover())
                    if b.get_label() == "Just this page")
    page_btn.emit("clicked")
    drain()
    assert allowed == {"uid": 1001, "url": "https://roblox.com/games", "whole": False}


def test_the_overview_says_when_nothing_was_blocked():
    page, _w, _u = _detail(activity=[])
    assert _rows(page.blocked_group)[0].get_title() == "Nothing blocked today"
    assert _rows(page.changes_group)[0].get_title() == "No changes this week"


def test_changes_to_the_account_name_who_made_them():
    now = int(time.time())
    events = [an_event(t=now - 100, kind="change", url="", why="", by=1000,
                       by_username="miriam", method="SetBlockedCategories",
                       args=[1001, ["adult", "sports"]], guardian=True)]
    page, _w, _u = _detail(activity=events)
    row = _rows(page.changes_group)[0]
    assert row.get_title() == "yosef: 2 kinds of site blocked"
    assert row.get_subtitle().startswith("miriam, ")


def test_a_waiting_request_for_this_person_is_on_their_overview():
    request = {"id": "a" * 32, "uid": 1001, "username": "yosef",
               "url": "https://example.com/x", "note": "", "mode": "filtered",
               "asked": 0}
    page, _w, _u = _detail(requests=[request])
    assert any(r.get_title().startswith("yosef asked for example.com")
               for r in _rows(page))


def test_the_category_grid_is_on_the_page_with_preset_all_and_none():
    saved = []

    class Recording(FakeClient):
        def set_blocked_categories(self, uid, cats, pw):
            saved.append(list(cats))

    win = FakeWindow(Recording())
    user = a_user(blocked_categories=["adult", "sports"])
    win.policy = {"revision": 1, "users": [user], "guardian": {"enabled": False},
                  "guest": {"enabled": False}, "adblock": {"enabled": True}}
    page = detail.UserDetailPage(win, user)
    drain()
    assert set(page.checks) == {"adult", "gambling", "sports", "ads"}
    assert page.checks["adult"].get_active() and page.checks["sports"].get_active()
    assert not page.checks["gambling"].get_active()
    labels_ = [b.get_label() for b in _buttons(page) if b.get_label()]
    assert "All" in labels_ and "None" in labels_
    assert any(l.endswith(" preset") for l in labels_)

    page.checks["gambling"].set_active(True)
    drain()
    assert saved[-1] == ["adult", "gambling", "sports"]
    page._set_categories(set())
    assert saved[-1] == []
    page._set_categories(set(page.checks))
    assert saved[-1] == ["ads", "adult", "gambling", "sports"]


def test_the_grid_marks_what_every_preset_blocks_and_what_was_added_here():
    page, _w, _u = _detail(blocked_categories=[*a_user()["blocked_categories"], "sports"])
    texts = _texts(page.grid)
    assert "every preset" in texts
    assert "added here" in texts
    assert "for everyone" in texts       # ads, the machine-wide Pi-hole setting


def test_no_preferences_page_is_nested_in_a_scrolled_window():
    # An Adw.PreferencesPage scrolls itself; nesting one in a
    # Gtk.ScrolledWindow squashes every row to zero height — the "empty
    # labels with toggles, only some visible" bug.
    def offenders(widget, inside_scroller=False, found=None):
        found = [] if found is None else found
        if isinstance(widget, Adw.PreferencesPage) and inside_scroller:
            found.append(widget)
        inside = inside_scroller or isinstance(widget, Gtk.ScrolledWindow)
        child = widget.get_first_child()
        while child is not None:
            offenders(child, inside, found)
            child = child.get_next_sibling()
        return found

    page, win, user = _detail()
    assert not offenders(page)
    win2, board = a_board([a_user()])
    assert not offenders(board)
    win3, activity = a_feed([])
    assert not offenders(activity)


# -- a control is greyed out only where the setting truly does nothing --------

@pytest.mark.parametrize("mode", ["whitelist", "dnsfilter"])
def test_pictures_and_language_stay_editable_where_they_still_act(mode):
    page, _w, _u = _detail(mode=mode)
    assert _row_named(page, "Pictures and video").get_sensitive(), mode
    assert _row_named(page, "Bad language").get_sensitive(), mode


@pytest.mark.parametrize("mode", ["none", "unfiltered"])
def test_they_are_greyed_out_where_nothing_applies(mode):
    page, _w, _u = _detail(mode=mode)
    assert not _row_named(page, "Pictures and video").get_sensitive(), mode
    assert not _row_named(page, "Bad language").get_sensitive(), mode


@pytest.mark.parametrize("mode", ["none", "whitelist", "dnsfilter", "unfiltered"])
def test_youtube_is_greyed_out_wherever_it_genuinely_cannot_act(mode):
    page, _w, _u = _detail(mode=mode)
    assert not _row_named(page, "Restricted Mode").get_sensitive(), mode


def test_everything_is_editable_in_filtered_mode():
    page, _w, _u = _detail(mode="filtered")
    for title in ("Pictures and video", "Bad language", "Restricted Mode", "Page rules"):
        assert _row_named(page, title).get_sensitive(), title


def test_a_greyed_out_row_says_why():
    page, _w, _u = _detail(mode="unfiltered")
    for title in ("Pictures and video", "Bad language"):
        row = _row_named(page, title)
        assert "enforces nothing" in row.get_subtitle(), title
    assert "Whitelist only" in _row_named(page, "Approved sites").get_subtitle()


# -- the youtube tab saves as you go --------------------------------------------

def test_the_youtube_kinds_have_all_and_none_and_save_at_once():
    saved = []

    class Recording(FakeClient):
        def set_youtube(self, uid, settings, pw):
            saved.append(settings)

    win = FakeWindow(Recording())
    user = a_user(youtube={"restrict": "moderate"})
    win.policy = {"revision": 1, "users": [user], "guardian": {"enabled": False},
                  "guest": {"enabled": False}, "adblock": {"enabled": True}}
    page = detail.UserDetailPage(win, user)
    drain()
    assert len(page.kind_switches) == len(labels.YOUTUBE_CATEGORIES)
    for code, row in page.kind_switches.items():
        assert row.get_title(), f"kind {code} has an empty label"
    assert saved == [], "building the tab must not save anything"
    page._set_all_kinds(True)
    assert saved[-1]["blocked_categories"] == sorted(labels.YOUTUBE_CATEGORIES)
    page._set_all_kinds(False)
    assert saved[-1] == {"restrict": "moderate"}


# -- things that had a daemon method and no way to reach it -------------------

def test_a_second_parent_can_be_made_an_administrator_from_the_app():
    promoted = {}

    class Recording(FakeClient):
        def set_user_admin(self, uid, admin, pw):
            promoted.update(uid=uid, admin=admin)

    win = FakeWindow(Recording())
    user = a_user(mode="unfiltered")
    win.policy = {"revision": 1, "users": [user], "guardian": {"enabled": False},
                  "guest": {"enabled": False}, "adblock": {"enabled": True}}
    page = detail.UserDetailPage(win, user)
    row = _row_named(page, "Administrator")
    assert not row.get_active()
    row.set_active(True)
    drain()
    assert promoted == {"uid": 1001, "admin": True}


def test_wifi_sign_in_opens_a_window_for_the_account():
    opened = {}

    class Recording(FakeClient):
        def set_captive_mode(self, uid, minutes):
            opened.update(uid=uid, minutes=minutes)

    win = FakeWindow(Recording())
    user = a_user(mode="whitelist")
    win.policy = {"revision": 1, "users": [user], "guardian": {"enabled": False},
                  "guest": {"enabled": False}, "adblock": {"enabled": True}}
    page = detail.UserDetailPage(win, user)
    _row_named(page, "Allow Wi-Fi sign-in").emit("activated")
    drain()
    assert opened == {"uid": 1001, "minutes": 10}


def test_an_unfiltered_account_is_not_offered_a_wifi_window():
    page, _w, _u = _detail(mode="unfiltered")
    assert not _row_named(page, "Allow Wi-Fi sign-in").get_sensitive()


def test_removing_an_account_asks_first():
    removed = []

    class Recording(FakeClient):
        def remove_user(self, uid):
            removed.append(uid)

    win = FakeWindow(Recording())
    dialogs.confirm_remove_user(win, a_user())
    drain()
    # Presenting the dialog must not delete anything on its own.
    assert removed == []


def test_the_detail_page_offers_the_familys_presets():
    from kosherd import profiles

    user = a_user(mode="filtered", blocked_categories=["adult", "sports"],
                  media_level="immodest")
    preset = profiles.to_dict(profiles.from_user(user, "Mine"))
    page, _w, _u = _detail(custom_profiles=[preset], blocked_categories=["adult", "sports"],
                           media_level="immodest")
    combo = _row_named(page, "Set up as")
    labels_ = [combo.get_model().get_string(i)
               for i in range(combo.get_model().get_n_items())]
    assert "Mine (yours)" in labels_
    assert labels_[combo.get_selected()] == "Mine (yours)"
    assert "Delete This Preset…" in {r.get_title() for r in _rows(page)}
    assert "Set up as Mine" in _texts(page)


# -- the words -------------------------------------------------------------------

def test_block_reasons_read_in_the_familys_words():
    assert labels.why_text("category:video,social") == "Video and streaming, Social networks"
    assert labels.why_text("rule:youtube.com/shorts*") == "a page rule (youtube.com/shorts*)"
    assert labels.why_text("content:nsfw") == "the page reads as explicit"
    assert labels.why_text("language") == "bad language on the page"
    assert labels.why_text("youtube:channel") == "not an approved channel"
    assert labels.why_text("") == ""


def test_relative_times_read_naturally():
    now = time.time()
    assert labels.when_text(int(now) - 20, now) == "just now"
    assert labels.when_text(int(now) - 1200, now) == "20 min ago"
    assert labels.when_text(int(now) - 7200, now) == "2 hours ago"
    assert labels.when_text(int(now) - 86400 * 2, now).startswith(
        time.strftime("%a", time.localtime(now - 86400 * 2)))


def test_the_window_module_still_exports_the_list_table():
    assert admin.EDITABLE_LISTS is labels.EDITABLE_LISTS


# -- going back to the previous version --------------------------------------------

def test_the_updates_page_names_the_version_it_would_go_back_to():
    win = FakeWindow()
    win.policy = {"revision": 1, "users": [], "guardian": {"enabled": False},
                  "guest": {"enabled": False}}
    page = family.UpdatesPage(win)
    drain()
    assert "2026.09.10" in page.version_row.get_subtitle()
    assert "Would return to version 2026.09.03" in page.back_row.get_subtitle()
    assert page.back_button.get_sensitive()


def test_without_a_previous_version_the_button_is_off_and_says_why():
    win = FakeWindow(FakeClient(deployment={"booted": {"version": "1"}, "rollback": None,
                                            "staged": None, "rollback_queued": False}))
    win.policy = {"revision": 1, "users": [], "guardian": {"enabled": False},
                  "guest": {"enabled": False}}
    page = family.UpdatesPage(win)
    drain()
    assert not page.back_button.get_sensitive()
    assert "no previous version" in page.back_row.get_subtitle()


def test_a_queued_rollback_is_not_offered_twice():
    win = FakeWindow(FakeClient(deployment={"booted": {"version": "2"},
                                            "rollback": {"version": "1"},
                                            "staged": None, "rollback_queued": True}))
    win.policy = {"revision": 1, "users": [], "guardian": {"enabled": False},
                  "guest": {"enabled": False}}
    page = family.UpdatesPage(win)
    drain()
    assert not page.back_button.get_sensitive()
    assert "Already going back" in page.back_row.get_subtitle()


def test_a_status_with_every_key_missing_still_renders():
    win = FakeWindow(FakeClient(deployment={}))
    win.policy = {"revision": 1, "users": [], "guardian": {"enabled": False},
                  "guest": {"enabled": False}}
    page = family.UpdatesPage(win)
    drain()
    assert page.version_row.get_subtitle() == "unknown"
    assert not page.back_button.get_sensitive()


def test_going_back_asks_first_and_then_calls_the_daemon():
    called = []

    class Recording(FakeClient):
        def rollback(self):
            called.append(True)

    win = FakeWindow(Recording())
    win.policy = {"revision": 1, "users": [], "guardian": {"enabled": False},
                  "guest": {"enabled": False}}
    page = family.UpdatesPage(win)
    drain()
    page._confirm_rollback()
    drain()
    # Presenting the confirmation must not roll anything back on its own.
    assert called == []


# -- the demo mode ------------------------------------------------------------------

def test_the_demo_client_answers_everything_the_real_client_does():
    # `just admin-demo` must not hit a screen that calls a method the
    # pretend daemon lacks.
    import inspect

    from kosherd.client import DaemonClient
    from kosherd.demo import DemoClient

    real = {n for n, _ in inspect.getmembers(DaemonClient, inspect.isfunction)
            if not n.startswith("_")}
    demo = {n for n, _ in inspect.getmembers(DemoClient, inspect.isfunction)
            if not n.startswith("_")}
    assert real <= demo, real - demo


def test_the_demo_family_drives_every_screen():
    from kosherd.demo import DemoClient

    client = DemoClient()
    win = FakeWindow(client)
    win.policy = client.get_policy()
    win.requests = client.list_requests()
    win.status = client.filter_status()
    win.summary = client.activity_summary()
    board = family.FamilyPage(win)
    board.refresh()
    drain()
    assert len(_children(board.cards)) == 6
    assert board.requests_banner.get_revealed()
    for user in win.policy["users"]:
        page = detail.UserDetailPage(win, user)
        drain()
        assert page.stack.get_pages().get_n_items() == 6, user["username"]
    activity = feed.ActivityPage(win)
    activity.refresh()
    drain()
    assert _rows(activity.feed), "the demo feed has entries"
    # A change made through the client shows up as a change event.
    client.set_media_level(1002, "all", "")
    assert client.list_activity()[0]["method"] == "SetMediaLevel"
    assert client.get_policy()["users"][2]["media_level"] == "all"



# -- the cursor says what can be clicked ----------------------------------------------

def test_cards_tiles_and_acting_rows_get_the_hand_cursor():
    win, page = a_board([a_user()])
    for child in _children(page.cards) + _children(page.tiles):
        assert child.get_cursor() is not None and child.get_cursor().get_name() == "pointer"
    detail_page, _w, _u = _detail()
    acting = [r for r in _rows(detail_page)
              if isinstance(r, Adw.ActionRow) and r.get_activatable()]
    assert acting, "the person's page has rows that act"
    for row in acting:
        assert row.get_cursor().get_name() == "pointer", row.get_title()
    # A row that only displays keeps the arrow.
    display_only = [r for r in _rows(detail_page)
                    if isinstance(r, Adw.ActionRow) and not r.get_activatable()]
    assert display_only and all(r.get_cursor() is None for r in display_only)
    for button in _buttons(detail_page)[:5]:
        assert button.get_cursor().get_name() == "pointer"


def test_turning_the_guest_on_opens_its_page():
    class Recording(FakeClient):
        def set_guest_config(self, enabled, mode, whitelist, pw):
            win.policy["guest"] = {"enabled": enabled, "uid": 1010, "mode": "whitelist",
                                   "whitelist": list(whitelist)}

    win = FakeWindow(Recording())
    win.policy = {"revision": 1, "users": [], "guardian": {"enabled": False},
                  "guest": {"enabled": False, "mode": "whitelist", "whitelist": []}}
    win.nav = type("Nav", (), {"pop_to_tag": lambda self, tag: None})()
    page = family.GuestPage(win)
    _row_named(page, "Guest account").set_active(True)
    drain()
    assert win.pushed[-1] == ("detail", 1010)

