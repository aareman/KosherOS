"""Look — your own desktop, chosen and tuned by you.

Open to every account and needing no password. Which desktop a person
signs in to is a preference, not a protection: the filter is per account
at the network layer and does not care which program draws the windows.
So the person picks for themselves here, and the daemon takes the account
from the D-Bus connection rather than from an argument, so this window can
only ever change the person in front of it.

Below that is one switch for the glass: the blurred wallpaper behind the
taskbar and the windows. It writes Blur My Shell's own settings for this
account, which the sign-in helper leaves alone, so it sticks. The rest of
the taskbar is tuned in Dash to Panel's own settings, a right-click on
the bar away.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import threading

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402

from kosherd.client import DaemonClient  # noqa: E402
from kosherd.policy import DEFAULT_LAYOUT, LAYOUTS  # noqa: E402

APP_ID = "org.kosherlinux.Look"

# Written for the person choosing, in the second person, with what each
# desktop feels like rather than what it is built from. Keys match
# kosherd.policy.LAYOUTS; a test holds them to it.
DESKTOPS = (
    ("classic", "Classic",
     "A taskbar along the bottom, an Apps button, windows you move and "
     "resize. What Windows, a Mac and a Chromebook have taught your hands."),
    ("tiling", "Tiling",
     "Windows arranged in a scrolling row, driven from the keyboard. The "
     "same settings, lock screen and accessibility tools as Classic."),
    ("advanced", "Advanced",
     "A separate niri session with the Noctalia shell, set up through text "
     "files you own. GNOME Settings is not available there."),
)
# Classic and Tiling are both GNOME and switch in place; Advanced is a
# different session and waits for the next sign-in.
IN_PLACE = {"classic", "tiling"}

TEXT = {
    "desktop": "Your desktop",
    "desktop_hint": ("Which desktop you sign in to. Changing it changes nothing "
                     "about how the internet is filtered for you."),
    "next_sign_in": "Your new desktop is ready at your next sign-in.",
    "sign_out": "Sign out",
    "glass": "Glass",
    "blur": "Blur behind the taskbar and windows",
    "blur_hint": "The wallpaper shows softly through them.",
    "rest": "Everything else",
    "rest_row": "Wallpaper, dark mode and text size",
    "rest_hint": "Settings does these well.",
    "open_settings": "Open Settings",
    "chosen": "{title} desktop",
    "loading": "Looking at your desktop…",
    "error_title": "Could not reach the desktop settings",
}


def _error_text(e: Exception) -> str:
    import re

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


def under_niri() -> bool:
    return "niri" in os.environ.get("XDG_CURRENT_DESKTOP", "").lower()


def apply_now(layout: str) -> None:
    """Write the layout's plan into the running GNOME session, the way the
    sign-in helper would, so the switch is seen at once. The helper stamps
    the layout, so the next sign-in leaves everything alone again."""
    subprocess.run(["kosher-layout", "--layout", layout], check=False,
                   capture_output=True, text=True, timeout=30)


def sign_out() -> None:
    if under_niri():
        subprocess.Popen(["niri", "msg", "action", "quit"])
    else:
        subprocess.Popen(["gnome-session-quit", "--logout"])


def open_appearance_settings() -> None:
    subprocess.Popen(["gnome-control-center", "background"])


# ---- the glass ---------------------------------------------------------------

BLUR_SCHEMA_DIR = "/usr/share/gnome-shell/extensions/blur-my-shell@aunetx/schemas"
BLUR_PARTS = ("panel", "applications")


def _settings(schema_id: str, directory: str | None = None):
    """A Gio.Settings for a schema, or None when the machine has no such
    schema (a development box without the extension, say): the window
    then leaves the control out rather than crashing on it."""
    source = Gio.SettingsSchemaSource.get_default()
    if directory is not None and os.path.isdir(directory):
        try:
            source = Gio.SettingsSchemaSource.new_from_directory(directory, source, False)
        except GLib.Error:
            return None
    schema = source.lookup(schema_id, True) if source is not None else None
    if schema is None:
        return None
    return Gio.Settings.new_full(schema, None, None)


class Glass:
    """Blur behind the taskbar and the windows, as one choice. Each part is
    Blur My Shell's own `blur` key for this account."""

    def __init__(self, parts=None):
        if parts is None:
            parts = [_settings(f"org.gnome.shell.extensions.blur-my-shell.{p}", BLUR_SCHEMA_DIR)
                     for p in BLUR_PARTS]
        self.parts = [p for p in parts if p is not None]

    @property
    def available(self) -> bool:
        return bool(self.parts)

    @property
    def blurred(self) -> bool:
        return any(p.get_boolean("blur") for p in self.parts)

    @blurred.setter
    def blurred(self, value: bool) -> None:
        for p in self.parts:
            p.set_boolean("blur", bool(value))


# ---- the window ------------------------------------------------------------

class Window(Adw.ApplicationWindow):
    def __init__(self, app: Adw.Application | None, *, client=None, glass=None,
                 apply=apply_now, in_niri: bool | None = None):
        super().__init__(application=app, title="Look",
                         default_width=560, default_height=700)
        self.client = client
        self.glass = glass if glass is not None else Glass()
        self.apply = apply
        self.in_niri = under_niri() if in_niri is None else in_niri
        self.layout = DEFAULT_LAYOUT
        self.toasts = Adw.ToastOverlay()
        self.set_content(self.toasts)
        self.view = Adw.ToolbarView()
        self.toasts.set_child(self.view)
        self.view.add_top_bar(Adw.HeaderBar())
        self.reload()

    def _client(self):
        if self.client is None:
            self.client = DaemonClient()
        return self.client

    def reload(self) -> None:
        self.view.set_content(Adw.StatusPage(
            title=TEXT["loading"], paintable=None,
            child=Gtk.Spinner(spinning=True, width_request=32, height_request=32)))
        _run_async(lambda: self._client().my_layout(), self._on_loaded, self._on_error)

    def _on_error(self, e: Exception) -> None:
        retry = Gtk.Button(label="Try again", halign=Gtk.Align.CENTER)
        retry.add_css_class("suggested-action")
        retry.connect("clicked", lambda _b: self.reload())
        self.view.set_content(Adw.StatusPage(
            icon_name="dialog-warning-symbolic", title=TEXT["error_title"],
            description=_error_text(e), child=retry))

    def _on_loaded(self, layout: str) -> None:
        self.layout = layout if layout in LAYOUTS else DEFAULT_LAYOUT
        self.view.set_content(self._build())

    # ---- the page ----------------------------------------------------------

    def _build(self) -> Gtk.Widget:
        page = Adw.PreferencesPage()
        page.add(self._desktop_group())
        self.glass_group = self._glass_group()
        if self.glass_group is not None:
            page.add(self.glass_group)
        page.add(self._rest_group())
        self._show_for(self.layout)
        return page

    def _desktop_group(self) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup(title=TEXT["desktop"], description=TEXT["desktop_hint"])
        self.banner = Adw.Banner(title=TEXT["next_sign_in"], button_label=TEXT["sign_out"])
        self.banner.connect("button-clicked", lambda _b: sign_out())
        group.add(self.banner)
        self.rows: dict[str, Adw.ActionRow] = {}
        self.checks: dict[str, Gtk.CheckButton] = {}
        first = None
        for key, title, words in DESKTOPS:
            check = Gtk.CheckButton(valign=Gtk.Align.CENTER, active=(key == self.layout))
            if first is None:
                first = check
            else:
                check.set_group(first)
            row = Adw.ActionRow(title=title, subtitle=words, activatable_widget=check)
            row.add_prefix(check)
            check.connect("toggled", self._on_toggled, key)
            group.add(row)
            self.rows[key] = row
            self.checks[key] = check
        return group

    def _on_toggled(self, check: Gtk.CheckButton, key: str) -> None:
        if not check.get_active() or key == self.layout:
            return
        previous, self.layout = self.layout, key
        for row in self.rows.values():
            row.set_sensitive(False)

        def work():
            self._client().set_my_layout(key)
            if key in IN_PLACE and not self.in_niri:
                self.apply(key)
            return key

        def done(_key):
            for row in self.rows.values():
                row.set_sensitive(True)
            self._show_for(key)
            title = next(t for k, t, _ in DESKTOPS if k == key)
            self.toasts.add_toast(Adw.Toast(title=TEXT["chosen"].format(title=title)))

        def failed(e):
            for row in self.rows.values():
                row.set_sensitive(True)
            self.layout = previous
            self.checks[previous].set_active(True)
            self.toasts.add_toast(Adw.Toast(title=_error_text(e)))

        _run_async(work, done, failed)

    def _show_for(self, key: str) -> None:
        """The glass switch while a GNOME desktop is running, and the
        sign-out banner when the running session is not the chosen one."""
        if self.glass_group is not None:
            self.glass_group.set_visible(not self.in_niri)
        running_is_chosen = (key not in IN_PLACE) == self.in_niri
        self.banner.set_revealed(not running_is_chosen)

    def _glass_group(self) -> Adw.PreferencesGroup | None:
        g = self.glass
        if not g.available:
            return None
        group = Adw.PreferencesGroup(title=TEXT["glass"])
        self.blur_row = Adw.SwitchRow(title=TEXT["blur"], subtitle=TEXT["blur_hint"],
                                      active=g.blurred)
        self.blur_row.connect("notify::active",
                              lambda r, _p: setattr(g, "blurred", r.get_active()))
        group.add(self.blur_row)
        return group

    def _rest_group(self) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup(title=TEXT["rest"])
        row = Adw.ActionRow(title=TEXT["rest_row"], subtitle=TEXT["rest_hint"])
        button = Gtk.Button(label=TEXT["open_settings"], valign=Gtk.Align.CENTER)
        button.connect("clicked", lambda _b: open_appearance_settings())
        button.set_sensitive(shutil.which("gnome-control-center") is not None)
        row.add_suffix(button)
        group.add(row)
        return group


class Application(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID)

    def do_activate(self):
        window = self.props.active_window or Window(self)
        window.present()


def main() -> int:
    return Application().run(None)
