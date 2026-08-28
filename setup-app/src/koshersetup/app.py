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
from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402

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


class Window(Adw.ApplicationWindow):
    def __init__(self, **kwargs):
        super().__init__(**kwargs, title="Welcome to KosherOS",
                         default_width=760, default_height=620)
        self.client = DaemonClient()
        self.admin_uid: int | None = None

        self.toasts = Adw.ToastOverlay()
        self.set_content(self.toasts)

        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.SLIDE_LEFT)
        self.back = Gtk.Button(label="Back", visible=False)
        self.back.connect("clicked", lambda _b: self._go("welcome"))
        self.next = Gtk.Button(label="Get Started")
        self.next.add_css_class("suggested-action")
        self.next.connect("clicked", lambda _b: self._advance())

        header = Adw.HeaderBar(show_end_title_buttons=False,
                               show_start_title_buttons=False)
        header.pack_start(self.back)
        header.pack_end(self.next)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.append(header)
        box.append(self.stack)
        self.toasts.set_child(box)

        self.stack.add_named(self._welcome_page(), "welcome")
        self.stack.add_named(self._admin_page(), "admin")
        self.stack.add_named(self._protect_page(), "protect")
        self.stack.add_named(self._firmware_page(), "firmware")
        self._go("welcome")

    # -- pages -------------------------------------------------------------

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
        self.password = Adw.PasswordEntryRow(title="Password")
        self.password2 = Adw.PasswordEntryRow(title="Confirm password")
        for row in (self.full_name, self.username, self.password, self.password2):
            group.add(row)
        self.full_name.connect("changed", self._suggest_username)
        return box

    def _protect_page(self) -> Gtk.Widget:
        box, group = _page(
            "Protection",
            "Both are optional and can be set later in KosherOS Admin.")

        self.guardian_switch = Adw.SwitchRow(
            title="Guardian password",
            subtitle="A second password — e.g. a spouse's — required to change "
                     "any filter setting, on top of the administrator's own")
        self.guardian_pw = Adw.PasswordEntryRow(title="Guardian password", sensitive=False)
        self.guardian_pw2 = Adw.PasswordEntryRow(title="Confirm", sensitive=False)
        self.guardian_switch.connect("notify::active", lambda s, _p: [
            w.set_sensitive(s.get_active()) for w in (self.guardian_pw, self.guardian_pw2)])

        self.grub_switch = Adw.SwitchRow(
            title="Boot menu password",
            subtitle="Stops the boot menu being edited to start the computer "
                     "without filtering")
        self.grub_pw = Adw.PasswordEntryRow(title="Boot password", sensitive=False)
        self.grub_pw2 = Adw.PasswordEntryRow(title="Confirm", sensitive=False)
        self.grub_switch.connect("notify::active", lambda s, _p: [
            w.set_sensitive(s.get_active()) for w in (self.grub_pw, self.grub_pw2)])

        for row in (self.guardian_switch, self.guardian_pw, self.guardian_pw2,
                    self.grub_switch, self.grub_pw, self.grub_pw2):
            group.add(row)
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

    def _go(self, name: str) -> None:
        self.stack.set_visible_child_name(name)
        self.back.set_visible(name in ("protect", "firmware"))
        self.next.set_label({"welcome": "Get Started", "admin": "Create Account",
                             "protect": "Continue", "firmware": "Finish"}[name])
        if name == "protect":
            self.back.set_visible(False)  # the account already exists

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
        password = self.password.get_text()
        if not USERNAME_RE.match(username):
            self.toast("Username must be lowercase letters, digits, - or _")
            return
        if len(password) < 6:
            self.toast("Password must be at least 6 characters")
            return
        if password != self.password2.get_text():
            self.toast("Passwords do not match")
            return

        self.next.set_sensitive(False)

        def on_done(uid):
            self.admin_uid = uid
            self.next.set_sensitive(True)
            self._go("protect")

        def on_error(e):
            self.next.set_sensitive(True)
            self.toast(_error_text(e))

        _run_async(lambda: self.client.create_first_admin(
            username, self.full_name.get_text().strip() or username, password),
            on_done, on_error)

    def _finish(self) -> None:
        guardian = grub = ""
        if self.guardian_switch.get_active():
            guardian = self.guardian_pw.get_text()
            if len(guardian) < 6 or guardian != self.guardian_pw2.get_text():
                self.toast("Guardian passwords must match and be 6+ characters")
                return
        if self.grub_switch.get_active():
            grub = self.grub_pw.get_text()
            if len(grub) < 6 or grub != self.grub_pw2.get_text():
                self.toast("Boot passwords must match and be 6+ characters")
                return

        self.next.set_sensitive(False)
        _run_async(lambda: self.client.finish_setup(guardian, grub),
                   lambda _r: self.get_application().quit(),
                   lambda e: (self.next.set_sensitive(True), self.toast(_error_text(e))))

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
