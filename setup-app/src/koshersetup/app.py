"""KosherOS first-boot wizard.

Four steps: welcome, administrator account, protection (guardian + boot
password), and a firmware checklist for the things an OS cannot enforce
about its own hardware. Runs full-screen on first boot; kosherd closes the
setup API permanently once this finishes.
"""

from __future__ import annotations

import re
import threading

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from kosherd.client import DaemonClient  # noqa: E402

APP_ID = "org.kosherlinux.Setup"
LOGO = "/usr/share/pixmaps/kosheros-logo.png"
USERNAME_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")


def _error_text(e: Exception) -> str:
    msg = re.sub(r"^.*?GDBus\.Error:[\w.]+: ", "", str(e))
    return re.sub(r" \(\d+\)$", "", msg)


def _run_async(work, on_done, on_error) -> None:
    def runner():
        try:
            result = work()
        except Exception as e:  # noqa: BLE001
            GLib.idle_add(on_error, e)
        else:
            GLib.idle_add(on_done, result)

    threading.Thread(target=runner, daemon=True).start()


def password_strength(pw: str) -> tuple[int, str]:
    """0..4 and a word. Deliberately simple and local: length matters most,
    then variety. A family setting a first password gets the same feedback a
    phone gives, not a lecture."""
    if not pw:
        return 0, ""
    score = 0
    if len(pw) >= 6:
        score += 1
    if len(pw) >= 10:
        score += 1
    classes = sum(bool(re.search(r, pw)) for r in (r"[a-z]", r"[A-Z]", r"\d", r"[^\w]"))
    if classes >= 2:
        score += 1
    if classes >= 3 and len(pw) >= 8:
        score += 1
    return score, ["Too short", "Weak", "Fair", "Good", "Strong"][score]


class PasswordPair:
    """Two password rows with live feedback: a strength bar under the first
    and a match indicator under the second. Both were missing, and the only
    feedback a person got was a toast AFTER pressing the button."""

    def __init__(self, group: Adw.PreferencesGroup, title: str,
                 confirm_title: str = "Confirm password", on_change=None):
        self.entry = Adw.PasswordEntryRow(title=title)
        self.confirm = Adw.PasswordEntryRow(title=confirm_title)
        self.bar = Gtk.LevelBar(min_value=0, max_value=4, margin_top=6,
                                margin_start=12, margin_end=12)
        self.bar.add_offset_value("weak", 1)
        self.bar.add_offset_value("fair", 2)
        self.bar.add_offset_value("good", 3)
        self.bar.add_offset_value("strong", 4)
        self.hint = Gtk.Label(halign=Gtk.Align.START, margin_start=12, margin_top=2)
        self.hint.add_css_class("caption")
        self.match = Gtk.Label(halign=Gtk.Align.START, margin_start=12, margin_top=2)
        self.match.add_css_class("caption")
        self.on_change = on_change
        for w in (self.entry, self.bar, self.hint, self.confirm, self.match):
            group.add(w)
        self.entry.connect("changed", lambda _e: self._update())
        self.confirm.connect("changed", lambda _e: self._update())
        self._update()

    def text(self) -> str:
        return self.entry.get_text()

    def set_sensitive(self, on: bool) -> None:
        for w in (self.entry, self.confirm, self.bar, self.hint, self.match):
            w.set_sensitive(on)

    def valid(self) -> bool:
        pw = self.entry.get_text()
        return len(pw) >= 6 and pw == self.confirm.get_text()

    def _update(self) -> None:
        pw, again = self.entry.get_text(), self.confirm.get_text()
        score, word = password_strength(pw)
        self.bar.set_value(score)
        self.hint.set_text(word if pw else "At least 6 characters")
        if not again:
            self.match.set_text("")
        elif pw == again:
            self.match.set_text("✓ Passwords match")
            self.match.remove_css_class("error")
            self.match.add_css_class("success")
        else:
            self.match.set_text("✗ Passwords do not match")
            self.match.remove_css_class("success")
            self.match.add_css_class("error")
        if self.on_change:
            self.on_change()


def _tab_goes_to(row: Gtk.Widget, target) -> None:
    """Make Tab from this row land on `target` (a widget, or a callable
    returning one). GTK4 has no focus-chain API, and tabbing out of a row
    inside an Adw.PreferencesGroup wanders into the list's own machinery —
    from the last field of a form, Tab must reach the button that submits
    it. Shift+Tab is left alone."""
    controller = Gtk.EventControllerKey()

    def on_key(_c, keyval, _keycode, state):
        if keyval in (Gdk.KEY_Tab, Gdk.KEY_KP_Tab) \
                and not (state & Gdk.ModifierType.SHIFT_MASK):
            widget = target() if callable(target) else target
            widget.grab_focus()
            return True
        return False

    controller.connect("key-pressed", on_key)
    row.add_controller(controller)


def _icons_out_of_tab_order(root: Gtk.Widget) -> None:
    """Take the small in-row icon buttons (the password peek eye, the edit
    pencil) out of the Tab chain. Tab should move to the NEXT FIELD; the
    icons remain mouse-clickable."""
    child = root.get_first_child()
    while child is not None:
        if isinstance(child, (Gtk.Button, Gtk.ToggleButton, Gtk.MenuButton)) \
                and not isinstance(child, Gtk.Switch):
            child.set_focusable(False)
        _icons_out_of_tab_order(child)
        child = child.get_next_sibling()


def _page(title: str, description: str) -> tuple[Gtk.Box, Adw.PreferencesGroup]:
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18,
                  margin_top=36, margin_bottom=36, margin_start=48, margin_end=48,
                  valign=Gtk.Align.CENTER)
    heading = Gtk.Label(label=title, halign=Gtk.Align.START)
    heading.add_css_class("title-1")
    sub = Gtk.Label(label=description, halign=Gtk.Align.START, wrap=True, xalign=0)
    sub.add_css_class("dim-label")
    group = Adw.PreferencesGroup()
    box.append(heading)
    box.append(sub)
    box.append(group)
    return box, group


# The wizard is read once, by somebody setting up a new computer, often on
# a laptop screen at arm's length and often by a parent who would rather
# not squint. Everything here is sized in em, so one number moves the whole
# wizard and the platform's own accessibility scaling still applies on top.
WIZARD_CSS = b"""
.kosher-wizard { font-size: 1.2em; }
.kosher-wizard .title-1 { font-size: 2.1em; }
.kosher-wizard .caption { font-size: 0.92em; }
.kosher-wizard button.pill { padding: 8px 26px; }
.kosher-wizard row { min-height: 52px; }
"""


def _load_css() -> None:
    display = Gdk.Display.get_default()
    if display is None:
        return
    provider = Gtk.CssProvider()
    provider.load_from_data(WIZARD_CSS)
    Gtk.StyleContext.add_provider_for_display(
        display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)


class Window(Adw.ApplicationWindow):
    def __init__(self, **kwargs):
        super().__init__(**kwargs, title="Welcome to KosherOS",
                         default_width=860, default_height=700)
        _load_css()
        self.add_css_class("kosher-wizard")
        self.client = DaemonClient()
        self.admin_uid: int | None = None

        self.toasts = Adw.ToastOverlay()
        self.set_content(self.toasts)

        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.SLIDE_LEFT,
                               vexpand=True)
        # Back bottom-left, Next bottom-right: where a wizard's feet go.
        # They used to live in the header bar, which reads as window chrome —
        # the person filling in the last field had to travel to the top of
        # the window to continue.
        self.back = Gtk.Button(label="Back", visible=False)
        self.back.connect("clicked", lambda _b: self._back())
        self.next = Gtk.Button(label="Get Started")
        self.next.add_css_class("suggested-action")
        self.next.add_css_class("pill")
        self.next.connect("clicked", lambda _b: self._advance())

        header = Adw.HeaderBar(show_end_title_buttons=False,
                               show_start_title_buttons=False)

        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL,
                          margin_top=12, margin_bottom=24,
                          margin_start=48, margin_end=48)
        actions.append(self.back)
        spring = Gtk.Box(hexpand=True)
        actions.append(spring)
        actions.append(self.next)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.append(header)
        box.append(self.stack)
        box.append(actions)
        self.toasts.set_child(box)

        self.stack.add_named(self._welcome_page(), "welcome")
        self.stack.add_named(self._admin_page(), "admin")
        self.stack.add_named(self._protect_page(), "protect")
        self.stack.add_named(self._firmware_page(), "firmware")
        self.stack.add_named(self._working_page(), "working")
        # Tab moves between FIELDS. Without this it stops on every row's
        # internal icon (the password "peek" eye and friends), which nobody
        # tabbing through a form wants; the icons stay mouse-clickable.
        _icons_out_of_tab_order(self.stack)
        # Resume: if a previous run already created the administrator (a
        # failed finish step, a crash, a reboot mid-wizard), do not ask for
        # a second one — the daemon refuses it anyway, which left a person
        # stuck at a page they could not pass.
        try:
            exists, who = self.client.admin_exists()
        except Exception:  # noqa: BLE001 - treat as "not yet"
            exists, who = False, ""
        if exists:
            self.toast(f"Administrator account '{who}' already exists — continuing")
            self._go("protect")
        else:
            self._go("welcome")

    # -- pages -------------------------------------------------------------

    def _working_page(self) -> Gtk.Widget:
        """A real loading screen for the slow steps, so the person always
        knows the machine is doing something rather than wondering whether
        their click took."""
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18,
                      valign=Gtk.Align.CENTER, halign=Gtk.Align.CENTER)
        self.spinner = Gtk.Spinner(width_request=48, height_request=48)
        self.working_label = Gtk.Label(label="Working…")
        self.working_label.add_css_class("title-2")
        self.working_detail = Gtk.Label(label="")
        self.working_detail.add_css_class("dim-label")
        box.append(self.spinner)
        box.append(self.working_label)
        box.append(self.working_detail)
        return box

    def _busy(self, title: str, detail: str = "") -> None:
        self._before_busy = self.stack.get_visible_child_name()
        self.working_label.set_text(title)
        self.working_detail.set_text(detail)
        self.spinner.start()
        self.back.set_visible(False)
        self.next.set_sensitive(False)
        self.stack.set_visible_child_name("working")

    def _unbusy(self, back_to: str | None = None) -> None:
        self.spinner.stop()
        self._go(back_to or self._before_busy)

    def _welcome_page(self) -> Gtk.Widget:
        page = Adw.StatusPage(
            title="Welcome to KosherOS",
            description="A filtered, family-safe computer.\n\n"
                        "Next you'll create the administrator account — the person "
                        "who manages profiles, filtering, and apps on this machine.")
        try:
            page.set_paintable(Gtk.Image.new_from_file(LOGO).get_paintable())
        except Exception:  # noqa: BLE001 - artwork is optional
            page.set_icon_name("security-high-symbolic")
        return page

    def _admin_page(self) -> Gtk.Widget:
        box, group = _page(
            "Administrator account",
            "This account manages the computer. It cannot bypass filtering — "
            "nobody on KosherOS has root access.")
        self.full_name = Adw.EntryRow(title="Full name")
        self.username = Adw.EntryRow(title="Username")
        # An installer or image may already have made a login account. Say
        # so and offer it, instead of letting the person discover it as
        # "'abba' already exists" after typing the name they wanted.
        try:
            existing = self.client.existing_accounts()
        except Exception:  # noqa: BLE001 - an older daemon
            existing = []
        if existing:
            names = ", ".join(existing)
            self.existing_note = Adw.ActionRow(
                title=f"This computer already has an account: {names}",
                subtitle="Enter its name to make it the administrator — the "
                         "password you set here becomes its password. Or "
                         "enter a new name to create a fresh account.")
            self.existing_note.add_prefix(Gtk.Image(icon_name="dialog-information-symbolic"))
            group.add(self.existing_note)
            self.username.set_text(existing[0])
        group.add(self.full_name)
        group.add(self.username)
        self.admin_pw = PasswordPair(group, "Password", on_change=self._revalidate)
        self.full_name.connect("changed", self._suggest_username)
        self.username.connect("changed", lambda _e: self._revalidate())
        # Enter walks the form: each field hands the keyboard to the next,
        # and the last one presses Create Account if everything is valid.
        self.full_name.connect("entry-activated", lambda _e: self.username.grab_focus())
        self.username.connect("entry-activated", lambda _e: self.admin_pw.entry.grab_focus())
        self.admin_pw.entry.connect("entry-activated",
                                    lambda _e: self.admin_pw.confirm.grab_focus())
        self.admin_pw.confirm.connect(
            "entry-activated",
            lambda _e: self._advance() if self._page_valid() else None)
        _tab_goes_to(self.admin_pw.confirm, lambda: self.next)
        return box

    def _protect_page(self) -> Gtk.Widget:
        box, group = _page(
            "Protection",
            "Both are optional and can be set later in KosherOS Admin.")

        self.guardian_switch = Adw.SwitchRow(
            title="Guardian password",
            subtitle="A second password — e.g. a spouse's — required to change "
                     "any filter setting, on top of the administrator's own")
        group.add(self.guardian_switch)
        self.guardian_pw = PasswordPair(group, "Guardian password", "Confirm",
                                        on_change=self._revalidate)
        self.guardian_pw.set_sensitive(False)
        self.guardian_switch.connect("notify::active", lambda s, _p: (
            self.guardian_pw.set_sensitive(s.get_active()), self._revalidate()))

        self.grub_switch = Adw.SwitchRow(
            title="Boot menu password",
            subtitle="Stops the boot menu being edited to start the computer "
                     "without filtering")
        group.add(self.grub_switch)
        self.grub_pw = PasswordPair(group, "Boot password", "Confirm",
                                    on_change=self._revalidate)
        self.grub_pw.set_sensitive(False)
        self.grub_switch.connect("notify::active", lambda s, _p: (
            self.grub_pw.set_sensitive(s.get_active()), self._revalidate()))
        _tab_goes_to(self.guardian_pw.confirm, lambda: self.grub_switch)
        _tab_goes_to(self.grub_pw.confirm, lambda: self.next)
        return box

    def _firmware_page(self) -> Gtk.Widget:
        box, group = _page(
            "One last step, outside KosherOS",
            "KosherOS protects this system once it starts. These firmware "
            "settings stop someone starting a different system instead — "
            "please set them in your computer's BIOS/UEFI menu.")
        for title, subtitle in [
            ("Set a firmware (BIOS/UEFI) password",
             "Otherwise anyone can change these settings back"),
            ("Disable booting from USB and network",
             "Otherwise another operating system can be started from a USB stick"),
            ("Keep Secure Boot enabled",
             "Ensures only signed system software starts"),
        ]:
            row = Adw.ActionRow(title=title, subtitle=subtitle)
            row.add_prefix(Gtk.Image(icon_name="emblem-important-symbolic"))
            group.add(row)
        return box

    # -- flow --------------------------------------------------------------

    def _suggest_username(self, entry) -> None:
        if self.username.get_text():
            return
        first = entry.get_text().strip().lower().split(" ")[0]
        cleaned = re.sub(r"[^a-z0-9_-]", "", first)
        if cleaned:
            self.username.set_text(cleaned)

    # Back goes to the PREVIOUS page. Not to the welcome page, which is what
    # it did, and not past the account step once the account exists.
    PAGES = ("welcome", "admin", "protect", "firmware")
    PREVIOUS = {"admin": "welcome", "firmware": "protect"}

    def _go(self, name: str) -> None:
        self.stack.set_visible_child_name(name)
        self.back.set_visible(name in self.PREVIOUS)
        self.next.set_visible(True)
        self.next.set_sensitive(True)
        self.next.set_label({"welcome": "Get Started", "admin": "Create Account",
                             "protect": "Continue", "firmware": "Finish"}[name])
        self._revalidate()
        # Put the keyboard where the person will type next — and on the
        # welcome page, on the button, so Enter starts the wizard: there is
        # nothing to type there, and a person who reads the screen and
        # presses Enter should not find nothing happens.
        if name == "admin":
            self.full_name.grab_focus()
        elif name == "welcome":
            self.next.grab_focus()

    def _back(self) -> None:
        name = self.stack.get_visible_child_name()
        if name in self.PREVIOUS:
            self.stack.set_transition_type(Gtk.StackTransitionType.SLIDE_RIGHT)
            self._go(self.PREVIOUS[name])
            self.stack.set_transition_type(Gtk.StackTransitionType.SLIDE_LEFT)

    def _page_valid(self) -> bool:
        """Can the person move on from the current page? Drives the button,
        so invalid input is visible BEFORE pressing it, not as a toast after."""
        page = self.stack.get_visible_child_name()
        if page == "admin":
            return bool(USERNAME_RE.match(self.username.get_text().strip())) \
                and self.admin_pw.valid()
        if page == "protect":
            return (not self.guardian_switch.get_active() or self.guardian_pw.valid()) \
                and (not self.grub_switch.get_active() or self.grub_pw.valid())
        return True

    def _revalidate(self) -> None:
        if hasattr(self, "next") and self.stack.get_visible_child_name():
            self.next.set_sensitive(self._page_valid())

    def _advance(self) -> None:
        page = self.stack.get_visible_child_name()
        if page == "welcome":
            self._go("admin")
        elif page == "admin":
            self._create_admin()
        elif page == "protect":
            self._go("firmware")
        else:
            self._finish()

    def _create_admin(self) -> None:
        username = self.username.get_text().strip()
        password = self.admin_pw.text()
        if not USERNAME_RE.match(username):
            self.toast("Username must be lowercase letters, digits, - or _")
            return
        if not self.admin_pw.valid():
            self.toast("Passwords must match and be at least 6 characters")
            return

        self._busy("Creating the account…",
                   f"Setting up '{username}' and applying the filter defaults")

        def on_done(uid):
            self.admin_uid = uid
            self._unbusy("protect")

        def on_error(e):
            self._unbusy("admin")
            self.toast(_error_text(e))

        _run_async(lambda: self.client.create_first_admin(
            username, self.full_name.get_text().strip() or username, password),
            on_done, on_error)

    def _finish(self) -> None:
        guardian = grub = ""
        if self.guardian_switch.get_active():
            if not self.guardian_pw.valid():
                self.toast("Guardian passwords must match and be 6+ characters")
                return
            guardian = self.guardian_pw.text()
        if self.grub_switch.get_active():
            if not self.grub_pw.valid():
                self.toast("Boot passwords must match and be 6+ characters")
                return
            grub = self.grub_pw.text()

        self._busy("Finishing setup…",
                   "Applying protection settings and starting the login screen")
        _run_async(lambda: self.client.finish_setup(guardian, grub),
                   lambda _r: self.get_application().quit(),
                   lambda e: (self._unbusy("protect"), self.toast(_error_text(e))))

    def toast(self, text: str) -> None:
        self.toasts.add_toast(Adw.Toast(title=text, timeout=5))


class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)

    def do_activate(self):
        (self.get_active_window() or Window(application=self)).present()


def main() -> int:
    # Nothing to do if the machine is already set up.
    try:
        if DaemonClient().setup_complete():
            return 0
    except Exception:  # noqa: BLE001 - show the wizard rather than block first boot
        pass
    return App().run(None)


if __name__ == "__main__":
    raise SystemExit(main())
