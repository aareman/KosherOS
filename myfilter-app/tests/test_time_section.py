"""Building My Filter's page for real, on a virtual display.

The one widget bug a source-reading test cannot see is the widget that
throws when it is built; so the page is built here from the same answer
the daemon gives, for a limited account, an unlimited one and an admin.
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

from kosherd import timelimits  # noqa: E402
from koshermyfilter import app  # noqa: E402


def _rows(widget, found=None):
    found = [] if found is None else found
    child = widget.get_first_child()
    while child is not None:
        if isinstance(child, Adw.PreferencesRow):
            found.append(child)
        _rows(child, found)
        child = child.get_next_sibling()
    return found


def _settings(**time):
    return {"managed": True, "username": "yosef", "mode": "filtered", "admin": False,
            "can_install_apps": False, "language_filter": "substitute", "adblock": True,
            "blocked_categories": [{"key": "adult", "label": "Adult"}],
            "media_level": "immodest", "youtube": {"restrict": "strict"},
            "time": {"admin": False, "limited": True, "daily_minutes": 120,
                     "allowed": timelimits.SCHEDULE_PRESETS["not_late"],
                     "today": timelimits.SCHEDULE_PRESETS["not_late"][0],
                     "used": 80 * 60, "left": 40 * 60, "block_ends": None, **time}}


def _page(settings):
    window = app.Window.__new__(app.Window)   # no daemon, no application
    return window._build(settings)


def test_a_limited_account_sees_its_limit_what_is_left_and_todays_hours():
    page = _page(_settings())
    rows = {r.get_title(): r for r in _rows(page)}
    assert "Time each day" in rows
    assert rows["Time each day"].get_subtitle() == "1 h 20 min used of 2 h · 40 min left today"
    assert rows["Today you can use this computer"].get_subtitle() == "06:00–21:00"


def test_a_spent_day_and_a_block_end_are_said_plainly():
    import time

    ends = int(time.time()) + 1800
    page = _page(_settings(used=120 * 60, left=0, block_ends=ends))
    rows = {r.get_title(): r for r in _rows(page)}
    assert rows["Time each day"].get_subtitle().endswith("Today's time is used up")
    assert rows["Today you can use this computer"].get_subtitle() == \
        "06:00–21:00 · Allowed until " + time.strftime("%H:%M", time.localtime(ends))


def test_no_limit_and_an_administrator_are_one_line_each():
    page = _page(_settings(limited=False, daily_minutes=0))
    titles = [r.get_title() for r in _rows(page)]
    assert "No time limit" in titles and "Time each day" not in titles
    page = _page(_settings(admin=True))
    assert any("administrator" in t for t in (r.get_title() for r in _rows(page)))


def test_an_answer_from_an_older_daemon_without_time_still_builds():
    settings = _settings()
    del settings["time"]
    page = _page(settings)
    assert "Your time" not in {r.get_title() for r in _rows(page)}
