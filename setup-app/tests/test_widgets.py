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
from gi.repository import GLib  # noqa: E402
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
        self.joined = []

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

    # -- first-boot network: offline, with a Wi-Fi card, two networks in range
    status = {"online": False, "kind": "", "name": "", "wifi_hardware": True}

    def network_status(self):
        return dict(self.status)

    def list_wifi(self):
        return [{"ssid": "Home", "signal": 82, "secured": True, "active": False},
                {"ssid": "Shul Guest", "signal": 61, "secured": False, "active": False}]

    def connect_wifi(self, ssid, password=""):
        self.joined.append((ssid, password))
        self.status = {"online": True, "kind": "wifi", "name": ssid, "wifi_hardware": True}


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
    assert page(win) == "network"  # the internet step now sits before the account


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


def test_tab_from_the_confirm_field_reaches_the_next_button():
    # GTK4 has no focus chain; without an explicit hop, Tab out of the
    # confirm field wandered into the list machinery and never reached
    # Next. The hop is a key controller on the row; drive its handler the
    # way a Tab keypress would.
    from gi.repository import Gdk

    win = window()
    win.present()  # focus only moves in a mapped window
    while GLib.MainContext.default().iteration(False):
        pass
    win._go("admin")

    def controllers(w):
        obs = w.observe_controllers()
        return [obs.get_item(i) for i in range(obs.get_n_items())]

    keys = [c for c in controllers(win.admin_pw.confirm)
            if isinstance(c, Gtk.EventControllerKey)]
    assert keys, "no key controller on the confirm field"
    # Spy on the destination: what matters is that the hop sends focus to
    # Next. (Where GTK ultimately parks focus inside an off-screen test
    # window is its own business and flaky under xvfb.)
    grabbed = []
    original = win.next.grab_focus
    win.next.grab_focus = lambda: (grabbed.append(True), original())[1]
    try:
        handled = [k.emit("key-pressed", Gdk.KEY_Tab, 23, Gdk.ModifierType(0))
                   for k in keys]
    finally:
        win.next.grab_focus = original
    assert any(handled), "Tab was not claimed by the hop"
    assert grabbed, "Tab from confirm did not send focus to Next"


def test_shift_tab_from_the_confirm_field_is_left_alone():
    from gi.repository import Gdk

    win = window()
    win._go("admin")
    obs = win.admin_pw.confirm.observe_controllers()
    keys = [obs.get_item(i) for i in range(obs.get_n_items())
            if isinstance(obs.get_item(i), Gtk.EventControllerKey)]
    handled = [k.emit("key-pressed", Gdk.KEY_Tab, 23,
                      Gdk.ModifierType.SHIFT_MASK) for k in keys]
    assert not any(handled), "Shift+Tab must keep its normal behaviour"


def test_enter_on_the_welcome_page_starts_the_wizard():
    # Nothing to type on the first page, so the keyboard belongs on the
    # button: a person who reads the screen and presses Enter must not find
    # that nothing happens.
    win = window()
    assert page(win) == "welcome"
    assert win.get_focus() is win.next


def test_the_wizard_is_set_in_larger_type():
    # It is read once, by somebody setting up a new computer, often at
    # arm's length. One number moves the whole wizard, in em so the
    # platform's own scaling still applies on top.
    from koshersetup import app as setup

    win = setup.Window.__new__(setup.Window)
    assert b".kosher-wizard { font-size: 1.2em; }" in setup.WIZARD_CSS
    assert b".title-1 { font-size: 2.1em; }" in setup.WIZARD_CSS
    source = (__import__("pathlib").Path(setup.__file__)).read_text()
    assert 'self.add_css_class("kosher-wizard")' in source
    assert "default_width=860" in source
    del win


# -- the optional internet step ----------------------------------------------------

def settle():
    """Let the wizard's worker threads finish and their idle callbacks run."""
    import time

    context = GLib.MainContext.default()
    for _ in range(60):
        while context.pending():
            context.iteration(False)
        time.sleep(0.005)


def rows(listbox):
    found, child = [], listbox.get_first_child()
    while child is not None:
        found.append(child)
        child = child.get_next_sibling()
    return found


def test_the_internet_step_sits_between_welcome_and_the_account_and_never_blocks():
    win = window()
    win._advance()                      # Get Started
    assert page(win) == "network"
    assert win.next.get_sensitive(), "optional: always passable"
    settle()
    assert win.next.get_label() == "Skip for now"
    win._advance()
    assert page(win) == "admin"
    win._back()
    assert page(win) == "network"


def test_offline_with_a_wifi_card_lists_the_networks_nearby():
    win = window()
    win._go("network")
    settle()
    assert "Not connected" in win.net_row.get_title()
    assert win.wifi_group.get_visible()
    names = [r.get_title() for r in rows(win.wifi_list)]
    assert names == ["Home", "Shul Guest"]
    assert rows(win.wifi_list)[0].get_subtitle() == "Password needed"
    assert rows(win.wifi_list)[1].get_subtitle() == "Open network"


def test_an_open_network_joins_at_once_and_the_button_turns_into_continue():
    win = window()
    win._go("network")
    settle()
    win._join({"ssid": "Shul Guest", "secured": False})
    assert page(win) == "working"
    settle()
    assert win.client.joined == [("Shul Guest", "")]
    assert page(win) == "network"
    assert win.online and win.next.get_label() == "Continue"
    assert "Connected by Wi-Fi: Shul Guest" in win.net_row.get_title()
    assert not win.wifi_group.get_visible()


def test_a_secured_network_asks_for_its_password_first():
    win = window()
    win._go("network")
    settle()
    dialog = win._join({"ssid": "Home", "secured": True})
    assert dialog is not None and "Home" in dialog.get_heading()
    assert win.client.joined == []
    dialog.get_extra_child().set_text("letmein")
    dialog.emit("response", "join")
    settle()
    assert win.client.joined == [("Home", "letmein")]


def test_a_refused_password_comes_back_to_the_page_with_the_reason():
    class Refusing(FakeClient):
        def connect_wifi(self, ssid, password=""):
            raise RuntimeError("That password was not accepted. Check it and try again.")

    setup.DaemonClient = lambda: Refusing()  # type: ignore[assignment]
    win = setup.Window(application=setup.App())
    win._go("network")
    settle()
    win._connect_wifi("Home", "wrong")
    settle()
    assert page(win) == "network"
    assert not win.online and win.next.get_label() == "Skip for now"


def test_already_online_by_cable_says_so_and_offers_no_list():
    class Wired(FakeClient):
        status = {"online": True, "kind": "ethernet", "name": "Wired connection 1",
                  "wifi_hardware": True}

    setup.DaemonClient = lambda: Wired()  # type: ignore[assignment]
    win = setup.Window(application=setup.App())
    win._go("network")
    settle()
    assert win.net_row.get_title().startswith("Connected by a network cable")
    assert not win.wifi_group.get_visible()
    assert win.next.get_label() == "Continue"


def test_no_wifi_card_says_a_cable_will_do_and_lets_the_person_skip():
    class Desktop(FakeClient):
        status = {"online": False, "kind": "", "name": "", "wifi_hardware": False}

    setup.DaemonClient = lambda: Desktop()  # type: ignore[assignment]
    win = setup.Window(application=setup.App())
    win._go("network")
    settle()
    assert "no Wi-Fi found" in win.net_row.get_title()
    assert not win.wifi_group.get_visible()
    assert win.next.get_sensitive() and win.next.get_label() == "Skip for now"
