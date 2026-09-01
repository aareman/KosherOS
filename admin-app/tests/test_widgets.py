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
