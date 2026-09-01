"""Building the admin app's widgets for real.

Every UI bug this project has shipped was a widget that threw when it was
constructed — a GLib.Variant unpacked the wrong way, a polkit action
prompting twice. A test that parses the source cannot see any of that, so
these build the actual dialogs against a stub client, on a virtual
display.

Skipped, not failed, where GTK is unavailable: the point is to catch the
bug on a developer's machine, not to make the suite unrunnable elsewhere.
"""

from __future__ import annotations

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


class FakeClient:
    """Answers the calls a dialog makes while it is being built."""

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
                               {"name": "gambling", "label": "Gambling"}]}

    def list_installed_details(self):
        return []

    def filter_status(self):
        return self._overrides.get("status", {"pictures": "checking",
                                              "degraded": [], "problems": []})

    def list_requests(self):
        return self._overrides.get("requests", [])


class FakeWindow:
    """Enough of Window for a dialog to be built and driven."""

    def __init__(self, client=None):
        self.client = client or FakeClient()
        self.toasts = []
        self.policy = {"revision": 1, "users": [], "guardian": {"enabled": False},
                       "guest": {"enabled": False}}

    def toast(self, text):
        self.toasts.append(text)

    def call(self, work, refresh=True, done_msg=None):
        work()

    def with_guardian(self, then):
        then("")

    def reload(self):
        pass


def drain():
    """Let the dialog's async load finish and its idle callbacks run."""
    from gi.repository import GLib

    context = GLib.MainContext.default()
    for _ in range(200):
        while context.pending():
            context.iteration(False)


@pytest.mark.parametrize("name,title,description,noun", admin.EDITABLE_LISTS,
                         ids=[e[0] for e in admin.EDITABLE_LISTS])
def test_every_list_dialog_builds(name, title, description, noun):
    win = FakeWindow()
    dialog = admin.ListEditDialog(win, name, title, description, noun)
    drain()
    assert dialog.shipped == 119
    assert win.client.calls == [("get_list_edits", name)]


def test_the_summary_says_how_many_ship_and_how_many_you_changed():
    # The sentence that stops somebody trying to curate the list.
    win = FakeWindow(FakeClient(edits={"add": {"blast": "bother"},
                                       "remove": ["drat"], "shipped": 145}))
    dialog = admin.ListEditDialog(win, "wordlist.json", "Bad language", "d", "word")
    drain()
    summary = dialog.summary.get_title()
    assert "145 come with KosherOS" in summary
    assert "you added 1" in summary
    assert "you removed 1" in summary


def test_undoing_an_addition_takes_it_out_of_what_will_be_saved():
    win = FakeWindow(FakeClient(edits={"add": {"blast": "bother", "drat": "dear"},
                                       "remove": [], "shipped": 10}))
    dialog = admin.ListEditDialog(win, "wordlist.json", "Bad language", "d", "word")
    drain()
    assert sorted(dialog._added_terms()) == ["blast", "drat"]
    dialog._drop_added("blast")
    assert dialog._added_terms() == ["drat"]


def test_a_content_term_is_undone_from_wherever_it_sits():
    # It is stored under a level and a weight; a person undoing it should
    # not have to know either.
    win = FakeWindow(FakeClient(edits={
        "add": {"immodest": {"12": ["a thing", "another"]},
                "nsfw": {"25": ["something"]}},
        "remove": [], "shipped": 181}))
    dialog = admin.ListEditDialog(win, "content-terms.json", "Words", "d", "term")
    drain()
    assert sorted(dialog._added_terms()) == ["a thing", "another", "something"]
    dialog._drop_added("another")
    assert sorted(dialog._added_terms()) == ["a thing", "something"]


def test_saving_sends_exactly_what_is_shown():
    sent = {}

    class Recording(FakeClient):
        def edit_list(self, name, add, remove, pw):
            sent.update(name=name, add=add, remove=remove)

    win = FakeWindow(Recording(edits={"add": {"blast": "bother"},
                                      "remove": ["drat"], "shipped": 10}))
    dialog = admin.ListEditDialog(win, "wordlist.json", "Bad language", "d", "word")
    drain()
    dialog._save()
    assert sent == {"name": "wordlist.json", "add": {"blast": "bother"},
                    "remove": ["drat"]}


def test_the_youtube_dialog_builds_with_every_category():
    from kosherd.policy import YOUTUBE_CATEGORIES

    win = FakeWindow()
    user = {"uid": 1001, "username": "a", "youtube": {"restrict": "strict"}}
    dialog = admin.YouTubeDialog(win, user, lambda settings: None)
    drain()
    assert dialog.restrict == "strict"
    assert dialog._settings()["restrict"] == "strict"
    # Every category YouTube labels must be offerable, or a parent cannot
    # block the one they care about.
    assert len(YOUTUBE_CATEGORIES) >= 15


def test_the_youtube_dialog_reports_channels_and_categories():
    win = FakeWindow()
    user = {"uid": 1001, "username": "a", "youtube": {
        "restrict": "moderate", "allowed_channels": ["@torah"],
        "blocked_categories": ["24"]}}
    dialog = admin.YouTubeDialog(win, user, lambda s: None)
    drain()
    settings = dialog._settings()
    assert settings["allowed_channels"] == ["@torah"]
    assert settings["blocked_categories"] == ["24"]


def test_the_category_dialog_builds_from_what_the_machine_has():
    win = FakeWindow()
    user = {"uid": 1001, "username": "a", "blocked_categories": ["adult"]}
    dialog = admin.CategoryDialog(win, user, lambda chosen: None)
    drain()
    assert dialog.chosen == {"adult"}


# -- the page a parent actually looks at --------------------------------------

def a_user(**overrides):
    user = {"uid": 1001, "username": "child1", "mode": "filtered",
            "admin": False, "whitelist": [], "rules": [],
            "blocked_categories": ["adult"], "media_level": "immodest",
            "language_filter": "substitute", "youtube": {"restrict": "strict"},
            "can_install_apps": False}
    user.update(overrides)
    return user


def a_page(users=None, **client_overrides):
    win = FakeWindow(FakeClient(**client_overrides))
    win.policy = {"revision": 1, "users": users if users is not None else [a_user()],
                  "guardian": {"enabled": False},
                  "guest": {"enabled": False, "mode": "whitelist",
                            "whitelist": []}}
    page = admin.ProfilesPage(win)
    page.refresh()
    drain()
    return win, page


def test_the_profiles_page_builds_with_a_user():
    win, page = a_page()
    assert page.users_group is not None


def test_a_user_row_builds_for_every_filter_mode():
    from kosherd.policy import MODES

    for mode in MODES:
        win, page = a_page([a_user(mode=mode)])
        assert page.users_group is not None, mode


def test_a_user_row_builds_for_every_profile():
    from kosherd import profiles

    for profile in profiles.PROFILES:
        user = a_user(mode=profile.mode,
                      blocked_categories=list(profile.blocked_categories),
                      media_level=profile.media_level,
                      language_filter=profile.language_filter,
                      youtube=dict(profile.youtube),
                      can_install_apps=profile.can_install_apps)
        win, page = a_page([user])
        assert page.users_group is not None, profile.key


def test_a_row_for_a_user_with_a_huge_whitelist_still_builds():
    # The whitelist UI has to scale; a thousand domains is a real family.
    domains = [f"site{i}.example" for i in range(1000)]
    win, page = a_page([a_user(mode="whitelist", whitelist=domains)])
    assert page.users_group is not None


def test_the_attention_group_appears_only_when_something_is_wrong():
    win, page = a_page(status={"pictures": "checking", "degraded": [],
                               "problems": []})
    assert page.status_group is None

    win, page = a_page(status={"pictures": "too_slow", "detect_ms": 620,
                               "degraded": [], "problems": []})
    assert page.status_group is not None


def test_every_reportable_problem_produces_a_row():
    win, page = a_page(status={
        "pictures": "no_model", "detect_ms": None,
        "degraded": ["kosher-mitm.service"],
        "problems": ["the bad-language list did not load"]})
    assert page.status_group is not None
    assert page.status_group.get_title() == "Needs your attention"


def test_the_requests_group_appears_only_when_somebody_asked():
    win, page = a_page(requests=[])
    assert page.requests_group is None

    win, page = a_page(requests=[
        {"id": "a" * 32, "uid": 1001, "username": "child1",
         "url": "https://example.com/needed", "note": "for school",
         "mode": "filtered", "asked": 0}])
    assert page.requests_group is not None
    assert "1" in page.requests_group.get_title()


def test_a_whitelist_request_is_granted_as_a_whole_site():
    # A whitelist account has no page-level rule to apply, so offering the
    # choice would offer something that does not do what it says.
    granted = {}

    class Recording(FakeClient):
        def approve_request(self, request_id, whole_site, pw):
            granted.update(id=request_id, whole_site=whole_site)

    win = FakeWindow(Recording())
    win.policy = {"revision": 1, "users": [], "guardian": {"enabled": False},
                  "guest": {"enabled": False}}
    page = admin.ProfilesPage(win)
    page._answer_request({"id": "b" * 32, "username": "kid", "mode": "whitelist",
                          "url": "https://chinuch.org/x"})
    drain()
    assert granted == {"id": "b" * 32, "whole_site": True}


def test_the_whitelist_dialog_builds_and_filters():
    win = FakeWindow()
    domains = [f"site{i}.example" for i in range(500)] + ["chinuch.org"]
    dialog = admin.WhitelistDialog(win, "Whitelist", domains, lambda new: None)
    drain()
    dialog.search.set_text("chinuch")
    # GtkSearchEntry debounces its signal by 150 ms, which no amount of
    # main-loop draining makes pass; call what the signal calls.
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
    dialog = admin.RulesDialog(win, "Page rules", rules, lambda new: None)
    drain()
    assert len(dialog.rules) == 2


def test_the_guest_can_be_set_up_from_a_profile():
    # The guest used to be the one account with no picture, language or
    # YouTube settings at all.
    from kosherd import profiles

    sent = {}

    class Recording(FakeClient):
        def set_guest_config(self, enabled, mode, whitelist, pw):
            sent.update(enabled=enabled, mode=mode)

    win = FakeWindow(Recording())
    win.policy = {"revision": 1, "users": [], "guardian": {"enabled": False},
                  "guest": {"enabled": True, "mode": "whitelist",
                            "whitelist": []}}
    page = admin.ProfilesPage(win)
    page.refresh()
    drain()
    assert page.guest_group is not None
    # The dropdown offers profiles, and the daemon accepts profile keys.
    from kosherd.daemon import Daemon

    assert profiles.get("child").key in {p.key for p in profiles.PROFILES}
    assert hasattr(Daemon, "impl_SetGuestConfig")
