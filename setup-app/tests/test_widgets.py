"""Build the first-boot setup window for real.

The graphical wizard is the path a parent on real hardware actually sees,
and until now nothing constructed it outside a booted machine — so a
widget that threw on construction would only ever surface as a frozen
first boot. These build it against a stub client on a virtual display.
"""

import pytest

gi = pytest.importorskip("gi")
try:
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Adw, Gtk
except ValueError:  # pragma: no cover - no typelibs
    pytest.skip("GTK4/libadwaita not available", allow_module_level=True)
if not Gtk.init_check():  # pragma: no cover - no display
    pytest.skip("no display", allow_module_level=True)
Adw.init()

from koshersetup import app as setup  # noqa: E402


class FakeClient:
    def setup_complete(self):
        return False

    def create_first_admin(self, username, full_name, password):
        return 1000

    def finish_setup(self, guardian, grub):
        pass


class FakeApp(Adw.Application):
    pass


def test_the_setup_window_builds():
    app = setup.App()
    win = setup.Window(application=app)
    win.client = FakeClient()
    assert win is not None


def test_the_app_class_constructs():
    app = setup.App()
    assert app.get_application_id() == setup.APP_ID
