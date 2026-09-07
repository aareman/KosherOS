"""Build the first-boot setup window for real and drive its flow.

The graphical wizard is the path a parent on real hardware actually sees,
and until now nothing constructed it outside a booted machine. The first
hands-on run found four things at once: Back jumped to the welcome page,
passwords gave no feedback, a failed finish step left the account created
but forced a second one, and the boot password could not be written. The
UI half of those is pinned here; the daemon half in kosherd/tests.
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
    def __init__(self, admin: str = ""):
        self.admin = admin
        self.created = []

    def setup_complete(self):
        return False

    def admin_exists(self):
        return (bool(self.admin), self.admin)

    def existing_accounts(self):
        return []

    def create_first_admin(self, username, full_name, password):
        self.created.append(username)
        return 1000

    def finish_setup(self, guardian, grub):
        pass


def window(admin: str = "") -> setup.Window:
    # The client is consulted in __init__ for resume, so it has to be in
    # place before the window is built.
    setup.DaemonClient = lambda: FakeClient(admin)  # type: ignore[assignment]
    return setup.Window(application=setup.App())


def page(win) -> str:
    return win.stack.get_visible_child_name()


def test_the_setup_window_builds():
    assert page(window()) == "welcome"


def test_back_goes_to_the_previous_page_not_the_start():
    win = window()
    win._go("firmware")
    win._back()
    assert page(win) == "protect", "Back from the last page must go one page back"
    win._go("admin")
    win._back()
    assert page(win) == "welcome"


def test_back_is_hidden_once_the_account_exists():
    # Nothing to go back TO: the account step is done and cannot be redone.
    win = window()
    win._go("protect")
    assert not win.back.get_visible()


def test_an_interrupted_wizard_resumes_past_the_account_step():
    # A failed finish step left "abba" created; the wizard must not demand a
    # second administrator.
    win = window(admin="abba")
    assert page(win) == "protect"


def test_password_strength_words():
    assert setup.password_strength("")[1] == ""
    assert setup.password_strength("abc")[1] == "Too short"
    assert setup.password_strength("abcdef")[0] >= 1
    strong = setup.password_strength("Correct-Horse-42")
    assert strong[0] == 4 and strong[1] == "Strong"


def test_password_feedback_is_live_and_gates_the_button():
    win = window()
    win._go("admin")
    win.username.set_text("abba")
    assert not win.next.get_sensitive(), "no password yet"

    win.admin_pw.entry.set_text("Correct-Horse-42")
    assert win.admin_pw.hint.get_text() == "Strong"
    assert win.admin_pw.bar.get_value() == 4
    assert not win.next.get_sensitive(), "not confirmed yet"

    win.admin_pw.confirm.set_text("Correct-Horse-4")
    assert "do not match" in win.admin_pw.match.get_text()
    assert not win.next.get_sensitive()

    win.admin_pw.confirm.set_text("Correct-Horse-42")
    assert "match" in win.admin_pw.match.get_text()
    assert "not" not in win.admin_pw.match.get_text()
    assert win.next.get_sensitive(), "valid input enables Create Account"


def test_a_bad_username_keeps_the_button_off():
    win = window()
    win._go("admin")
    win.admin_pw.entry.set_text("Correct-Horse-42")
    win.admin_pw.confirm.set_text("Correct-Horse-42")
    win.username.set_text("Abba Areman")
    assert not win.next.get_sensitive()


def test_optional_protections_only_gate_when_switched_on():
    win = window()
    win._go("protect")
    assert win.next.get_sensitive(), "both off: nothing to validate"
    win.grub_switch.set_active(True)
    assert not win.next.get_sensitive(), "on but empty"
    win.grub_pw.entry.set_text("boot-pass-1")
    win.grub_pw.confirm.set_text("boot-pass-1")
    assert win.next.get_sensitive()


def test_a_pre_existing_account_is_offered_not_hidden():
    # The dev disk used to bake in "abba"; an installer or kickstart may
    # do the same. The wizard must say so and offer it.
    class WithAccount(FakeClient):
        def existing_accounts(self):
            return ["abba"]

    setup.DaemonClient = lambda: WithAccount()  # type: ignore[assignment]
    win = setup.Window(application=setup.App())
    assert win.username.get_text() == "abba"
    assert "abba" in win.existing_note.get_title()


# -- the hands-on UX round: button placement, tab order, loading screens ------

def _tab_stops(root):
    """Every focusable widget under root, in tree order."""
    stops = []

    def walk(w):
        if w.get_focusable():
            stops.append(w)
        c = w.get_first_child()
        while c is not None:
            walk(c)
            c = c.get_next_sibling()

    walk(root)
    return stops


def test_no_in_row_icon_sits_in_the_tab_chain():
    # Tab must travel field -> field. The password rows' peek eyes and the
    # entry rows' edit icons used to be stops, so tabbing out of a field
    # landed on a button nobody wanted.
    win = window()
    win._go("admin")
    for stop in _tab_stops(win.stack):
        assert not isinstance(stop, (Gtk.Button, Gtk.ToggleButton)), \
            f"a {type(stop).__name__} inside the form is still tabbable"


def test_next_and_back_live_in_the_bottom_action_bar_not_the_header():
    win = window()
    # Walking up from the button must reach the window WITHOUT passing a
    # header bar: the actions live at the bottom of the content box.
    w = win.next
    while w is not None:
        assert not isinstance(w, Adw.HeaderBar), "Next is still in the header"
        w = w.get_parent()


def test_creating_the_account_shows_a_loading_screen():
    class SlowClient(FakeClient):
        def __init__(self):
            super().__init__()
            self.finished = False

    win = window()
    win._go("admin")
    win.username.set_text("abba")
    win.admin_pw.entry.set_text("Correct-Horse-42")
    win.admin_pw.confirm.set_text("Correct-Horse-42")
    win._busy("Creating the account…", "detail")
    assert page(win) == "working"
    assert win.spinner.get_spinning()
    assert "Creating" in win.working_label.get_text()
    assert not win.next.get_sensitive(), "nothing to press while working"
    win._unbusy("protect")
    assert page(win) == "protect"
    assert not win.spinner.get_spinning()


def test_a_failure_returns_to_the_page_that_was_being_filled_in():
    win = window()
    win._go("admin")
    win._busy("Creating the account…")
    win._unbusy("admin")
    assert page(win) == "admin"


def test_enter_walks_the_admin_form():
    # The wiring exists: each field's activation hands focus onward, and the
    # confirm field submits when the page is valid.
    win = window()
    win._go("admin")
    win.full_name.set_text("Abba")
    win.username.set_text("abba")
    win.admin_pw.entry.set_text("Correct-Horse-42")
    win.admin_pw.confirm.set_text("Correct-Horse-42")
    assert win._page_valid()
    win.admin_pw.confirm.emit("entry-activated")
    # _advance on a valid admin page calls create_first_admin via the fake
    # and lands on the working page (async completion needs a main loop).
    assert page(win) in ("working", "protect")
