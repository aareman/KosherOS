"""These tests build real windows. Make sure nobody sees them.

Loaded before any test module imports Gtk, this gives the process a virtual
display of its own (see kosherd.virtualdisplay). Without it GTK draws on
the developer's desktop — even under xvfb-run on a Wayland session, since
GTK4 prefers Wayland and xvfb-run only replaces DISPLAY.
"""

import pytest

from kosherd import virtualdisplay

DISPLAY = virtualdisplay.ensure()


@pytest.fixture(autouse=True)
def _own_state_dir(tmp_path, monkeypatch):
    """When Activity was last looked at lives in the account's state
    folder (seen.py); a test must never write the developer's."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))


def pytest_report_header(config):
    return f"display: {DISPLAY or 'none'}"


def pytest_configure(config):
    if DISPLAY is None:
        config.issue_config_time_warning(pytest.PytestConfigWarning(
            "Xvfb is not installed, so the widget tests are not collected rather "
            "than opening windows on your desktop. The dev shell provides it: "
            "re-enter `devenv shell`."), stacklevel=2)


def pytest_ignore_collect(collection_path, config):
    # GTK4's init_check says yes with no display and the first widget then
    # raises, so the modules' own "no display" skip never fires. Decline
    # the files here instead, before anything imports Gtk.
    if DISPLAY is None and collection_path.suffix == ".py":
        return True
    return None
