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
from kosheradmin import (computer, detail, dialogs, family, feed,  # noqa: E402
                         labels, schedule, sidebar)


class FakeClient:
    """Answers the calls a page makes while it is being built."""

    def __init__(self, **overrides):
        self.calls = []
        self._overrides = overrides

    def get_list_edits(self, name):
        self.calls.append(("get_list_edits", name))
        return self._overrides.get("edits",
                                   {"add": {}, "remove": [], "shipped": 119})

    def list_whitelist_bundles(self):
        return self._overrides.get("bundles", [
            {"key": "torah", "label": "Torah study",
             "description": "Sefaria, YUTorah, TorahAnytime and the rest", "domains": 38},
            {"key": "mail-and-files", "label": "Email and files",
             "description": "Gmail, Outlook, OneDrive, Drive, Dropbox", "domains": 40},
        ])

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
        self.time_usage = state.get("time_usage", {})
        self.catalog_count = state.get("catalog_count")
        self.update_state = None
        self.pushed = []
        self.shown_activity = []
        self.went_to = []
        self.destination = "family"

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

    def go_to(self, key):
        self.went_to.append(key)
        self.destination = key

    def pop_to_root(self):
        self.went_to.append("root")

    def lock(self):
        self.went_to.append("lock")

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


def test_the_mode_is_a_badge_on_the_card_and_the_persons_page():
    # The most important thing to see, so it is drawn rather than read:
    # blue while the account is filtered, amber when it is not.
    win, page = a_board([a_user(mode="filtered"), a_user(uid=1002, username="avi",
                                                          mode="unfiltered")])
    cards = _children(page.cards)
    filtered = next(w for w in _walk(cards[0]) if w.has_css_class("mode-badge"))
    assert filtered.get_label() == "Filtered internet"
    assert not filtered.has_css_class("open")
    unfiltered = next(w for w in _walk(cards[1]) if w.has_css_class("mode-badge"))
    assert unfiltered.get_label() == "Not filtered"
    assert unfiltered.has_css_class("open")

    detail_page, _w, _u = _detail(mode="whitelist")
    badge = next(w for w in _walk(detail_page) if w.has_css_class("mode-badge"))
    assert badge.get_label() == "Approved sites only"
    assert badge.has_css_class("big"), "the person's page shows it large"


def test_what_is_blocked_reads_blue_and_what_is_open_reads_amber():
    page, _w, _u = _detail(mode="filtered", media_level="none",
                           youtube={"restrict": "none"})
    chips = [w for w in _walk(page) if w.has_css_class("chip")]
    blocking = [c for c in chips if c.has_css_class("blocking")]
    open_ = [c for c in chips if c.has_css_class("open")]
    assert blocking and open_, "both kinds are on this account"
    texts = {_chip_text(c) for c in open_}
    assert any("pictures shown" in t for t in texts)
    # An unfiltered account's web line is amber too.
    page, _w, _u = _detail(mode="unfiltered")
    open_ = [c for c in _walk(page) if c.has_css_class("chip") and c.has_css_class("open")]
    assert any("Unfiltered internet" == _chip_text(c) for c in open_)


def _walk(widget, found=None):
    found = [] if found is None else found
    child = widget.get_first_child()
    while child is not None:
        found.append(child)
        _walk(child, found)
        child = child.get_next_sibling()
    return found


def _chip_text(chip):
    return " ".join(w.get_label() for w in _walk(chip) if isinstance(w, Gtk.Label))


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
    # The board stays quiet when there is nothing wrong; the sidebar is
    # where a healthy filter says so, on every page rather than only here.
    win, page = a_board([a_user()])
    assert not page.health_banner.get_revealed()
    rail = sidebar.Sidebar(win)
    rail.refresh()
    drain()
    assert rail.rows["protection"].status.get_label() == "Running"
    assert not rail.rows["protection"].badge.get_visible()


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


def test_the_sidebar_lists_the_people_and_the_computer_and_says_how_each_is():
    win = FakeWindow(FakeClient(), catalog_count=23)
    win.policy = {"revision": 1, "users": [a_user(), a_user(uid=1002, username="rivky")],
                  "guardian": {"enabled": False}, "adblock": {"enabled": True},
                  "guest": {"enabled": False}}
    win.requests = [{"id": "a" * 32, "uid": 1001, "username": "yosef", "mode": "filtered",
                     "url": "https://x.test/a", "asked": 0}]
    rail = sidebar.Sidebar(win)
    rail.refresh()
    drain()
    assert [key for key, _t, _i, _s in sidebar.DESTINATIONS] == \
        ["family", "activity", "protection", "apps", "updates"]
    sections = [s for _k, _t, _i, s in sidebar.DESTINATIONS if s]
    assert sections == ["The family", "This computer"]
    assert rail.rows["family"].status.get_label() == "2 people"
    assert rail.rows["family"].badge.get_label() == "1"      # one request waiting
    assert rail.rows["apps"].status.get_label() == "23 apps"
    assert rail.rows["protection"].status.get_label() == "Running"
    # Clicking a row takes the window there.
    rail.list.select_row(rail.rows["updates"])
    drain()
    assert win.went_to == ["updates"]


def test_the_sidebar_shows_a_filter_problem_as_an_amber_badge():
    win = FakeWindow(FakeClient(), status={"pictures": "no_model", "detect_ms": None,
                                           "degraded": ["kosher-dns.service"],
                                           "problems": [], "services": {}})
    rail = sidebar.Sidebar(win)
    rail.refresh()
    drain()
    assert rail.rows["protection"].badge.get_visible()
    assert rail.rows["protection"].badge.get_label() == "2 problems"
    assert "warn" in rail.rows["protection"].badge.get_css_classes()


def test_protection_is_one_page_with_the_health_the_ads_and_the_guardian():
    win = FakeWindow(FakeClient())
    win.policy = {"revision": 1, "users": [a_user()], "guardian": {"enabled": True},
                  "adblock": {"enabled": True}, "guest": {"enabled": False}}
    page = computer.ProtectionPage(win)
    drain()
    titles = [r.get_title() for r in _rows(page)]
    assert "The filter is running" in titles            # the health, not behind a dialog
    assert "Block ads and trackers" in titles
    for _n, list_title, _d, _u in labels.EDITABLE_LISTS:  # the word lists, inline
        assert list_title in titles
    guardian = _row_named(page, "Guardian password")
    assert guardian.get_subtitle().startswith("On.")


def test_switching_ad_blocking_off_goes_through_the_guardian():
    asked = []

    class Recording(FakeClient):
        def set_adblock(self, enabled, pw=""):
            asked.append(enabled)

    win = FakeWindow(Recording())
    win.policy = {"revision": 1, "users": [], "guardian": {"enabled": False},
                  "adblock": {"enabled": True}, "guest": {"enabled": False}}
    page = computer.ProtectionPage(win)
    page.ads_row.set_active(False)
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
    assert names == ["overview", "filtering", "media", "youtube", "time", "account"]
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


def test_the_detail_page_has_seven_tabs_and_starts_on_the_overview():
    page, _w, _u = _detail()
    names = []
    stack_pages = page.stack.get_pages()
    for i in range(stack_pages.get_n_items()):
        names.append(stack_pages.get_item(i).get_name())
    assert names == ["overview", "filtering", "media", "youtube", "time", "apps", "account"]
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
    page = computer.UpdatesPage(win)
    drain()
    assert "2026.09.10" in page.version_row.get_subtitle()
    assert "Would return to version 2026.09.03" in page.back_row.get_subtitle()
    assert page.back_button.get_sensitive()


def test_without_a_previous_version_the_button_is_off_and_says_why():
    win = FakeWindow(FakeClient(deployment={"booted": {"version": "1"}, "rollback": None,
                                            "staged": None, "rollback_queued": False}))
    win.policy = {"revision": 1, "users": [], "guardian": {"enabled": False},
                  "guest": {"enabled": False}}
    page = computer.UpdatesPage(win)
    drain()
    assert not page.back_button.get_sensitive()
    assert "no previous version" in page.back_row.get_subtitle()


def test_a_queued_rollback_is_not_offered_twice():
    win = FakeWindow(FakeClient(deployment={"booted": {"version": "2"},
                                            "rollback": {"version": "1"},
                                            "staged": None, "rollback_queued": True}))
    win.policy = {"revision": 1, "users": [], "guardian": {"enabled": False},
                  "guest": {"enabled": False}}
    page = computer.UpdatesPage(win)
    drain()
    assert not page.back_button.get_sensitive()
    assert "Already going back" in page.back_row.get_subtitle()


def test_a_status_with_every_key_missing_still_renders():
    win = FakeWindow(FakeClient(deployment={}))
    win.policy = {"revision": 1, "users": [], "guardian": {"enabled": False},
                  "guest": {"enabled": False}}
    page = computer.UpdatesPage(win)
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
    page = computer.UpdatesPage(win)
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
    win.time_usage = client.time_usage()
    board = family.FamilyPage(win)
    board.refresh()
    drain()
    assert len(_children(board.cards)) == 6
    assert any("used of 2 h" in t for t in _texts(board)), "yosef's card shows today's time"
    assert board.requests_banner.get_revealed()
    for user in win.policy["users"]:
        page = detail.UserDetailPage(win, user)
        drain()
        assert page.stack.get_pages().get_n_items() == 7, user["username"]
    activity = feed.ActivityPage(win)
    activity.refresh()
    drain()
    assert _rows(activity.feed), "the demo feed has entries"
    # A change made through the client shows up as a change event.
    client.set_media_level(1002, "all", "")
    assert client.list_activity()[0]["method"] == "SetMediaLevel"
    assert client.get_policy()["users"][2]["media_level"] == "all"


def test_the_real_window_walks_every_sidebar_destination():
    """The window, not a stand-in.

    Every page has its own test, but the thing that broke when the app
    grew a sidebar was the wiring between them — which page the content
    pane holds, which row is lit, and whether a person still pushes on
    top. So this builds the actual Window against the demo daemon and
    walks it.
    """
    from kosherd.demo import DemoClient

    original, admin.DaemonClient = admin.DaemonClient, DemoClient
    was_admin = admin.Window._not_an_admin
    admin.Window._not_an_admin = lambda self: False
    try:
        win = admin.Window()
        drain()
        assert win.policy["users"], "the demo family loaded"
        for key, title, _icon, _section in sidebar.DESTINATIONS:
            win.go_to(key)
            drain()
            page = win.nav.get_visible_page()
            assert page is not None, key
            assert win.sidebar.rows[key].is_selected(), key
            assert win.destination == key
        # The sidebar says how each destination is, without being opened.
        assert win.sidebar.rows["apps"].status.get_label().endswith("apps")
        assert win.sidebar.rows["family"].status.get_label().endswith("people")
        # A person pushes on top of the board, and backs out to it.
        win.go_to("family")
        win.open_user_detail(win.policy["users"][0])
        drain()
        assert isinstance(win.nav.get_visible_page(), detail.UserDetailPage)
        win.pop_to_root()
        drain()
        assert win.nav.get_visible_page().get_tag() == "family"
        # The health banner's Details is the Protection page now.
        win.family_page.health_banner.emit("button-clicked")
        drain()
        assert win.destination == "protection"
    finally:
        admin.DaemonClient = original
        admin.Window._not_an_admin = was_admin


# -- the cursor says what can be clicked ----------------------------------------------

def test_cards_sidebar_rows_and_acting_rows_get_the_hand_cursor():
    win, page = a_board([a_user()])
    for child in _children(page.cards):
        assert child.get_cursor() is not None and child.get_cursor().get_name() == "pointer"
    for row in sidebar.Sidebar(win).rows.values():
        assert row.get_cursor().get_name() == "pointer"
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



# -- Enter submits, everywhere a password or a name is typed -------------------------

def _entries(widget, found=None):
    found = [] if found is None else found
    child = widget.get_first_child()
    while child is not None:
        if isinstance(child, (Gtk.Entry, Gtk.PasswordEntry, Adw.EntryRow)):
            found.append(child)
        _entries(child, found)
        child = child.get_next_sibling()
    return found


def test_enter_in_the_guardian_password_dialog_sets_it():
    # Reaching for the mouse to press Continue is exactly the friction that
    # makes a second password annoying enough to switch off.
    set_to = []

    class Recording(FakeClient):
        def set_guardian_password(self, old, new):
            set_to.append((old, new))

    win = FakeWindow(Recording())
    win.policy = {"revision": 1, "users": [], "guardian": {"enabled": False},
                  "guest": {"enabled": False}}
    dialog = dialogs.guardian_dialog(win)
    drain()
    entries = _entries(dialog)
    assert entries, "the dialog has a field to type into"
    entries[-1].set_text("a secret")
    entries[-1].emit("activate")
    drain()
    assert set_to == [("", "a secret")]


def test_every_password_and_name_dialog_submits_on_enter():
    import inspect

    from kosheradmin import common, detail as detail_mod, dialogs as dialogs_mod

    # One helper does it, and every dialog that takes typed text uses it.
    assert "activates-default" in inspect.getsource(common.submit_on_enter)
    for module in (admin, dialogs_mod, detail_mod):
        source = inspect.getsource(module)
        for marker in ("add_response(", ):
            pass
        if "submit_on_enter(" in source:
            assert "set_default_response(" in source, module.__name__
    guardian = inspect.getsource(dialogs_mod.guardian_dialog)
    assert 'submit_on_enter(dialog, "ok", old, new)' in guardian
    unlock = inspect.getsource(admin.Window.with_guardian)
    assert 'submit_on_enter(dialog, "ok", entry)' in unlock
    assert "entry.grab_focus()" in unlock


def test_the_helper_answers_the_dialog_and_closes_it():
    from kosheradmin.common import submit_on_enter

    dialog = Adw.AlertDialog(heading="Guardian password")
    dialog.add_response("cancel", "Cancel")
    dialog.add_response("ok", "Continue")
    answers = []
    dialog.connect("response", lambda _d, r: answers.append(r))
    entry = Gtk.PasswordEntry()
    submit_on_enter(dialog, "ok", entry)
    assert entry.get_property("activates-default")
    entry.emit("activate")
    drain()
    assert answers == ["ok"]


def test_the_persons_header_carries_only_the_name():
    # The subtitle under the name repeated what the page says directly
    # below, in a space too narrow for it, and crowded the tabs.
    page, _w, user = _detail()
    titles = [w for w in _walk(page) if isinstance(w, Adw.WindowTitle)]
    assert titles and titles[0].get_title() == user["username"]
    assert titles[0].get_subtitle() == ""
    # ...and the mode sits beside the name on the page itself.
    badge = next(w for w in _walk(page) if w.has_css_class("mode-badge"))
    row = badge.get_parent()
    assert any(isinstance(w, Gtk.Label) and w.has_css_class("title-2")
               for w in _walk(row)), "the name and the mode share a line"


# -- whitelist mode: lists, not categories -------------------------------------------

def test_a_whitelist_account_is_not_asked_which_kinds_of_site_to_block():
    # It reaches its approved list and nothing else, so fifteen category
    # toggles would change nothing at all.
    page, _w, _u = _detail(mode="whitelist")
    drain()
    titles = {r.get_title() for r in _rows(page)}
    assert "Blocked kinds of sites" not in {g.get_title() for g in _groups(page)}
    assert "Approved sites" in {g.get_title() for g in _groups(page)}
    assert "Torah study" in titles and "Email and files" in titles
    assert "This family's own list" in titles


def test_a_filtered_account_still_gets_the_category_grid():
    page, _w, _u = _detail(mode="filtered")
    drain()
    groups = {g.get_title() for g in _groups(page)}
    assert "Blocked kinds of sites" in groups
    assert "Approved sites" not in groups


def _groups(widget, found=None):
    found = [] if found is None else found
    child = widget.get_first_child()
    while child is not None:
        if isinstance(child, Adw.PreferencesGroup):
            found.append(child)
        _groups(child, found)
        child = child.get_next_sibling()
    return found


def test_switching_a_ready_made_list_on_saves_it():
    saved = []

    class Recording(FakeClient):
        def set_whitelist_bundles(self, uid, bundles, pw):
            saved.append((uid, list(bundles)))

    win = FakeWindow(Recording())
    user = a_user(mode="whitelist", whitelist=["ourfamily.example"])
    win.policy = {"revision": 1, "users": [user], "guardian": {"enabled": False},
                  "guest": {"enabled": False}, "adblock": {"enabled": True}}
    page = detail.UserDetailPage(win, user)
    drain()
    _row_named(page, "Torah study").set_active(True)
    drain()
    assert saved == [(1001, ["torah"])]
    _row_named(page, "Email and files").set_active(True)
    drain()
    assert saved[-1] == (1001, ["mail-and-files", "torah"]), "both can be on at once"


def test_the_card_says_how_a_whitelist_account_is_set_up():
    user = a_user(mode="whitelist", whitelist=["a.example", "b.example"],
                  whitelist_bundles=["torah", "mail-and-files"])
    lines = dict((text, protects) for _icon, text, protects
                 in labels.protection_lines(user))
    assert "Only 2 approved lists and 2 sites" in lines
    only_lists = labels.protection_lines(a_user(mode="whitelist",
                                                whitelist_bundles=["torah"]))[0][1]
    assert only_lists == "Only 1 approved list of sites"



# -- time: the tab, the calendar, the card ------------------------------------------

def _time_page(users=None, usage=None, **kw):
    saved = []

    class Recording(FakeClient):
        def set_time_limits(self, uid, settings, pw=""):
            saved.append((uid, settings))

    win = FakeWindow(Recording())
    user = a_user(**kw)
    win.policy = {"revision": 1, "users": users or [user], "guardian": {"enabled": False},
                  "guest": {"enabled": False}, "adblock": {"enabled": True}}
    win.time_usage = usage or {}
    page = detail.UserDetailPage(win, user)
    drain()
    return page, saved, user


def test_the_time_tab_has_the_limit_the_presets_and_the_calendar():
    page, saved, _u = _time_page()
    titles = {r.get_title() for r in _rows(page)}
    assert "Daily limit" in titles and "Minutes a day" in titles
    combo = _row_named(page, "Daily limit")
    options = [combo.get_model().get_string(i) for i in range(combo.get_model().get_n_items())]
    assert options == ["No daily limit", "30 minutes a day", "1 hour a day", "2 hours a day",
                       "3 hours a day", "A different amount…"]
    assert options[combo.get_selected()] == "No daily limit", "nothing is set until you set it"
    assert not _row_named(page, "Minutes a day").get_visible()
    labels_ = [b.get_label() for b in _buttons(page) if b.get_label()]
    for preset in ("Always", "After school", "Not late at night", "Weekdays only"):
        assert preset in labels_, preset
    assert isinstance(page.schedule_grid, schedule.ScheduleGrid)
    assert page.schedule_grid.get_grid() == ["1" * 24] * 7
    assert saved == [], "building the tab must not save anything"


def test_picking_a_daily_limit_saves_it_and_the_change_reads_well():
    page, saved, _u = _time_page()
    combo = _row_named(page, "Daily limit")
    combo.set_selected(3)                                   # 2 hours a day
    drain()
    assert saved == [(1001, {"daily_minutes": 120})]
    assert labels.time_summary(saved[0][1]) == "2 h a day, any hour", "what the toast says"
    combo.set_selected(0)                                   # no limit
    drain()
    assert saved[-1] == (1001, {})


def test_a_custom_amount_shows_the_spinner_and_saves_once_the_clicking_stops():
    from gi.repository import GLib

    page, saved, _u = _time_page()
    combo = _row_named(page, "Daily limit")
    combo.set_selected(5)                                   # a different amount
    drain()
    spin = _row_named(page, "Minutes a day")
    assert spin.get_visible()
    assert saved[-1] == (1001, {"daily_minutes": 90}), "the spinner's value is what is saved"
    spin.set_value(95)
    spin.set_value(100)
    drain()
    assert saved[-1] == (1001, {"daily_minutes": 90}), "not yet: the clicks are still coming"
    done = []
    GLib.timeout_add(900, lambda: done.append(True) or False)
    while not done:
        GLib.MainContext.default().iteration(True)
    assert saved[-1] == (1001, {"daily_minutes": 100})


def test_a_schedule_preset_paints_the_calendar_and_saves():
    from kosherd import timelimits

    page, saved, _u = _time_page()
    after_school = next(b for b in _buttons(page) if b.get_label() == "After school")
    after_school.emit("clicked")
    drain()
    assert page.schedule_grid.get_grid() == timelimits.SCHEDULE_PRESETS["after_school"]
    assert saved == [(1001, {"allowed": timelimits.SCHEDULE_PRESETS["after_school"]})]
    always = next(b for b in _buttons(page) if b.get_label() == "Always")
    always.emit("clicked")
    drain()
    assert saved[-1] == (1001, {})


def test_an_existing_limit_and_schedule_are_shown_as_set():
    from kosherd import timelimits

    page, _s, _u = _time_page(time={"daily_minutes": 60,
                                    "allowed": timelimits.SCHEDULE_PRESETS["not_late"]},
                              usage={"1001": {"used": 80 * 60, "limit": 3600, "left": 0,
                                              "blocked": True, "reason": "limit",
                                              "allowed_now": True, "block_ends": None,
                                              "next_allowed": None, "limited": True,
                                              "signed_in": True}})
    combo = _row_named(page, "Daily limit")
    assert combo.get_model().get_string(combo.get_selected()) == "1 hour a day"
    assert page.schedule_grid.get_grid() == timelimits.SCHEDULE_PRESETS["not_late"]
    today = _rows(page)[0] if False else page.time_today_row
    assert today.get_title().startswith("1 h 20 min used of 1 h")
    assert "Signed in now" in today.get_subtitle()


def test_a_blocked_account_is_told_when_it_comes_back():
    now = time.time()
    tomorrow_six = time.mktime((*time.localtime(now + 86400)[:3], 6, 0, 0, 0, 0, -1))
    page, _s, _u = _time_page(time={"daily_minutes": 60},
                              usage={"1001": {"used": 3600, "limit": 3600, "left": 0,
                                              "blocked": True, "reason": "limit",
                                              "allowed_now": True, "block_ends": None,
                                              "next_allowed": int(tomorrow_six),
                                              "limited": True, "signed_in": False}})
    assert page.time_today_row.get_subtitle() == "Not allowed right now, until tomorrow 06:00"
    assert labels.until_text(int(now) + 60, now) == time.strftime("%H:%M", time.localtime(now + 60))
    assert labels.until_text(int(now) + 3 * 86400, now).startswith(
        time.strftime("%a ", time.localtime(now + 3 * 86400)))


def test_an_administrator_cannot_be_limited_and_the_tab_says_so():
    page, _s, _u = _time_page(admin=True)
    titles = {r.get_title() for r in _rows(page)}
    assert "Daily limit" not in titles and "Administrator" in titles
    assert any("never limited" in (r.get_subtitle() or "") for r in _rows(page)
               if isinstance(r, Adw.ActionRow))
    assert not hasattr(page, "schedule_grid")


# -- the calendar widget itself ---------------------------------------------------------

def _painted(grid_widget):
    """Render the grid to an image and return it."""
    import cairo

    width, height = 400, schedule.HEADER + 24 * schedule.CELL_HEIGHT + 4
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, width, height)
    cr = cairo.Context(surface)
    grid_widget._draw(grid_widget, cr, width, height)
    return surface


def test_the_calendar_draws_allowed_and_blocked_hours_in_two_colours():
    from kosherd import timelimits

    widget = schedule.ScheduleGrid(timelimits.SCHEDULE_PRESETS["not_late"])
    widget.set_size_request(400, 0)
    surface = _painted(widget)
    data = surface.get_data()
    stride = surface.get_stride()

    def pixel(x, y):
        offset = y * stride + x * 4
        return tuple(data[offset:offset + 4])

    # The widget has no allocation off-screen, so use its content geometry.
    column_width = (400 - schedule.LEFT) / 7
    x = int(schedule.LEFT + column_width * 1.5)
    allowed = pixel(x, schedule.HEADER + 10 * schedule.CELL_HEIGHT + 8)   # 10:00
    blocked = pixel(x, schedule.HEADER + 23 * schedule.CELL_HEIGHT + 8)   # 23:00
    assert allowed != blocked
    # BGRA: the allowed (amber) cell is redder than blue; the blocked one bluer.
    assert allowed[2] > allowed[0] and blocked[0] > blocked[2]


def test_dragging_paints_a_rectangle_and_saves_once():
    changes = []
    widget = schedule.ScheduleGrid(["1" * 24] * 7, on_change=changes.append)
    widget.set_size_request(400, 0)
    # Off-screen the widget has no width, so geometry is driven by hand.
    widget._column_width = lambda: 50.0
    x0 = schedule.LEFT + 25              # Sunday column
    y0 = schedule.HEADER + 8 * schedule.CELL_HEIGHT + 3   # 08:00

    class Gesture:
        def get_start_point(self):
            return True, x0, y0

    widget._on_drag_begin(Gesture(), x0, y0)
    assert widget.cell(0, 8) == "0", "the first cell flips at once"
    widget._on_drag_update(Gesture(), 2 * 50, 3 * schedule.CELL_HEIGHT)   # to Tue 11:00
    widget._on_drag_end(Gesture(), 2 * 50, 3 * schedule.CELL_HEIGHT)
    assert changes and len(changes) == 1, "one save per drag"
    grid = changes[0]
    sunday, monday, tuesday = grid[6], grid[0], grid[1]
    for day in (sunday, monday, tuesday):
        assert day[8:12] == "0000" and day[:8] == "1" * 8 and day[12:] == "1" * 12
    assert grid[2] == "1" * 24, "Wednesday was not touched"
    # A press off the cells does nothing.
    widget._on_drag_begin(Gesture(), 2, 2)
    widget._on_drag_end(Gesture(), 0, 0)
    assert len(changes) == 1


def test_the_keyboard_moves_a_cursor_and_space_flips_the_hour():
    from gi.repository import Gdk

    changes = []
    widget = schedule.ScheduleGrid(["1" * 24] * 7, on_change=changes.append)
    assert widget.get_focusable()
    widget._on_key(None, Gdk.KEY_Right, 0, 0)
    widget._on_key(None, Gdk.KEY_Down, 0, 0)
    assert widget.cursor == (1, 9)
    widget._on_key(None, Gdk.KEY_space, 0, 0)
    assert widget.cell(1, 9) == "0"
    assert changes[-1][0][9] == "0", "column 1 as shown is Monday in the policy"
    assert not widget._on_key(None, Gdk.KEY_a, 0, 0)


def test_the_card_carries_a_fifth_line_for_time():
    from kosherd import timelimits

    limited = a_user(time={"daily_minutes": 120})
    win, page = a_board([limited, a_user(uid=1002, username="rivky")],
                        time_usage={"1001": {"used": 80 * 60, "limit": 7200, "left": 2400,
                                             "limited": True, "signed_in": True}})
    cards = _children(page.cards)
    texts = _texts(cards[0])
    assert "1 h 20 min used of 2 h" in texts
    apps = next(i for i, t in enumerate(texts) if "app" in t.lower())
    clock = texts.index("1 h 20 min used of 2 h")
    assert apps < clock, "after the four fixed lines"
    assert "No daily limit" in _texts(cards[1])
    # Amber when nothing limits, blue when something does.
    text, protects = labels.time_line(limited, None)
    assert protects and text == "0 min used of 2 h"
    text, protects = labels.time_line(a_user(), None)
    assert not protects and text == "No daily limit"
    text, protects = labels.time_line(a_user(admin=True), None)
    assert not protects and "administrator" in text
    text, _p = labels.time_line(a_user(time={"allowed": timelimits.SCHEDULE_PRESETS["weekdays"]}),
                                {"used": 600})
    assert text.startswith("10 min today, no daily limit")


def test_a_time_event_reads_in_the_feed_and_on_the_overview():
    now = int(time.time())
    events = [an_event(t=now - 4, kind="time", url="", why="time:login"),
              an_event(t=now - 5, kind="time", url="", why="time:limit")]
    win, page = a_feed(events)
    titles = [r.get_title() for r in _rows(page.feed)]
    assert titles == ["Refused a sign-in outside the allowed time",
                      "Signed out: today's time was used up"]
    page, _w, _u = _detail(activity=events)
    titles = [r.get_title() for r in _rows(page.blocked_group)]
    assert "Signed out: today's time was used up" in titles
    assert labels.why_text("time:schedule") == "the allowed hours ended"
    what, _d = labels.change_sentence({"method": "SetTimeLimits", "username": "yosef",
                                       "args": [1001, {"daily_minutes": 60}], "t": now})
    assert what == "yosef: time → 1 h a day, any hour"
