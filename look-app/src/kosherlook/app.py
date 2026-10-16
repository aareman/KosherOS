"""Look — your own desktop, chosen and tuned by you.

Open to every account and needing no password. Which desktop a person
signs in to is a preference, not a protection: the filter is per account
at the network layer and does not care which program draws the windows.
So the person picks for themselves here, and the daemon takes the account
from the D-Bus connection rather than from an argument, so this window can
only ever change the person in front of it.

The second half is the classic taskbar's look: how see-through it is,
whether the wallpaper is blurred behind it, its size. Those are written
into the extensions' own settings for this account, where the sign-in
helper leaves them alone (it writes its plan when the layout changes, not
at every sign-in), so what is chosen here sticks.
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

# The classic taskbar's size, in GNOME logical pixels. Medium is the
# image's default.
SIZES = (("Small", 40), ("Medium", 48), ("Large", 56))

TEXT = {
    "desktop": "Your desktop",
    "desktop_hint": ("Which desktop you sign in to. Changing it changes nothing "
                     "about how the internet is filtered for you."),
    "next_sign_in": "Your new desktop is ready at your next sign-in.",
    "sign_out": "Sign out",
    "taskbar": "Taskbar",
    "taskbar_hint": "The bar along the bottom of the Classic desktop.",
    "see_through": "See-through",
    "see_through_hint": "How much of the wallpaper shows through the bar.",
    "blur": "Blur the wallpaper behind it",
    "size": "Size",
    "reset": "Back to the KosherOS look",
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


# ---- the taskbar's own settings ------------------------------------------

D2P_SCHEMA = "org.gnome.shell.extensions.dash-to-panel"
BLUR_SCHEMA = "org.gnome.shell.extensions.blur-my-shell.panel"
BLUR_SCHEMA_DIR = "/usr/share/gnome-shell/extensions/blur-my-shell@aunetx/schemas"


def _settings(schema_id: str, directory: str | None = None):
    """A Gio.Settings for a schema, or None when the machine has no such
    schema (a development box without the extensions, say): the window
    then leaves that control out rather than crashing on it."""
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


class Taskbar:
    """The classic taskbar's look, read and written through the extensions'
    own settings for this account. Opacity is 0..1 as Dash to Panel keeps
    it; the window shows it as a percentage."""

    OPACITY_KEYS = ("trans-use-custom-opacity", "trans-panel-opacity")
    SIZE_KEYS = ("panel-size", "panel-sizes")

    def __init__(self, panel=None, blur=None):
        self.panel = panel if panel is not None else _settings(D2P_SCHEMA)
        self.blur = blur if blur is not None else _settings(BLUR_SCHEMA, BLUR_SCHEMA_DIR)

    @property
    def available(self) -> bool:
        return self.panel is not None

    @property
    def opacity(self) -> float:
        return float(self.panel.get_double("trans-panel-opacity"))

    @opacity.setter
    def opacity(self, value: float) -> None:
        self.panel.set_boolean("trans-use-custom-opacity", True)
        self.panel.set_double("trans-panel-opacity", max(0.0, min(1.0, value)))

    @property
    def blurred(self) -> bool:
        return bool(self.blur.get_boolean("blur")) if self.blur is not None else False

    @blurred.setter
    def blurred(self, value: bool) -> None:
        if self.blur is not None:
            self.blur.set_boolean("blur", bool(value))

    @property
    def size(self) -> int:
        return int(self.panel.get_int("panel-size"))

    @size.setter
    def size(self, value: int) -> None:
        self.panel.set_int("panel-size", int(value))
        # Newer Dash to Panel keeps a per-monitor table that wins over the
        # single value; clearing it lets the one size apply everywhere.
        self.panel.reset("panel-sizes")

    def reset(self) -> None:
        """Back to the image's defaults: the keys are unset, so the
        machine-wide dconf values show through again."""
        for key in self.OPACITY_KEYS + self.SIZE_KEYS:
            self.panel.reset(key)
        if self.blur is not None:
            self.blur.reset("blur")


# ---- the window ------------------------------------------------------------

class Window(Adw.ApplicationWindow):
    def __init__(self, app: Adw.Application | None, *, client=None, taskbar=None,
                 apply=apply_now, in_niri: bool | None = None):
        super().__init__(application=app, title="Look",
                         default_width=560, default_height=700)
        self.client = client
        self.taskbar = taskbar if taskbar is not None else Taskbar()
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
        self.taskbar_group = self._taskbar_group()
        if self.taskbar_group is not None:
            page.add(self.taskbar_group)
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
        """What follows from the chosen desktop: the taskbar controls for
        Classic, and the sign-out banner when the running session is not
        the chosen one."""
        if self.taskbar_group is not None:
            self.taskbar_group.set_visible(key == "classic" and not self.in_niri)
        running_is_chosen = (key not in IN_PLACE) == self.in_niri
        self.banner.set_revealed(not running_is_chosen)

    def _taskbar_group(self) -> Adw.PreferencesGroup | None:
        t = self.taskbar
        if not t.available:
            return None
        group = Adw.PreferencesGroup(title=TEXT["taskbar"], description=TEXT["taskbar_hint"])

        self.opacity = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 100, 5)
        self.opacity.set_hexpand(True)
        self.opacity.set_size_request(200, -1)
        self.opacity.set_draw_value(False)
        self.opacity.set_value(round((1.0 - t.opacity) * 100))
        self.opacity.connect("value-changed",
                             lambda s: setattr(t, "opacity", 1.0 - s.get_value() / 100))
        row = Adw.ActionRow(title=TEXT["see_through"], subtitle=TEXT["see_through_hint"])
        row.add_suffix(self.opacity)
        group.add(row)

        if t.blur is not None:
            self.blur_row = Adw.SwitchRow(title=TEXT["blur"], active=t.blurred)
            self.blur_row.connect("notify::active",
                                  lambda r, _p: setattr(t, "blurred", r.get_active()))
            group.add(self.blur_row)
        else:
            self.blur_row = None

        self.size_row = Adw.ComboRow(title=TEXT["size"],
                                     model=Gtk.StringList.new([name for name, _ in SIZES]))
        self.size_row.set_selected(min(range(len(SIZES)),
                                       key=lambda i: abs(SIZES[i][1] - t.size)))
        self.size_row.connect("notify::selected",
                              lambda r, _p: setattr(t, "size", SIZES[r.get_selected()][1]))
        group.add(self.size_row)

        reset = Gtk.Button(label=TEXT["reset"], halign=Gtk.Align.END, margin_top=12)
        reset.connect("clicked", lambda _b: self._reset_taskbar())
        self.reset_button = reset
        group.add(reset)
        return group

    def _reset_taskbar(self) -> None:
        t = self.taskbar
        t.reset()
        self.opacity.set_value(round((1.0 - t.opacity) * 100))
        if self.blur_row is not None:
            self.blur_row.set_active(t.blurred)
        self.size_row.set_selected(min(range(len(SIZES)),
                                       key=lambda i: abs(SIZES[i][1] - t.size)))

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
