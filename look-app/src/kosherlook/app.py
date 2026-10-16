"""Look — your own desktop, chosen and tuned by you.

Open to every account and needing no password. Which desktop a person
signs in to is a preference, not a protection: the filter is per account
at the network layer and does not care which program draws the windows.
So the person picks for themselves here, and the daemon takes the account
from the D-Bus connection rather than from an argument, so this window can
only ever change the person in front of it.

Below that is the glass: the blurred wallpaper behind the taskbar and
the windows, on or off, how much, and how see-through the windows are. It writes Blur My Shell's own settings for this
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
    "how_much": "How much",
    "how_much_hint": "How soft the wallpaper looks behind them.",
    "transparency": "Transparency",
    "transparency_hint": "How much of the wallpaper shows through a window.",
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


BLUR_EFFECTS = ("native_static_gaussian_blur", "gaussian_blur")
# The blur radius the "How much" slider runs over, in logical pixels; the
# image ships 20. Transparency is how much of the blurred wallpaper shows
# through a window, as a fraction; past half, text gets hard to read.
RADIUS_RANGE = (2.0, 50.0)
MAX_TRANSPARENCY = 0.5


def _effects_with_radius(effects: GLib.Variant, radius: float) -> GLib.Variant:
    """A pipeline's `effects` (av of a{sv}) with every blur's radius set,
    every other value kept exactly as it was typed."""
    out = []
    for i in range(effects.n_children()):
        effect = effects.get_child_value(i).get_variant()
        fields = {}
        is_blur = False
        for j in range(effect.n_children()):
            entry = effect.get_child_value(j)
            key = entry.get_child_value(0).get_string()
            fields[key] = entry.get_child_value(1).get_variant()
            if key == "type" and fields[key].get_string() in BLUR_EFFECTS:
                is_blur = True
        if is_blur and "params" in fields:
            params = {}
            p = fields["params"]
            for j in range(p.n_children()):
                entry = p.get_child_value(j)
                params[entry.get_child_value(0).get_string()] = entry.get_child_value(1).get_variant()
            params["unscaled_radius"] = GLib.Variant("d", float(radius))
            params.pop("radius", None)
            fields["params"] = GLib.Variant("a{sv}", params)
        out.append(GLib.Variant("a{sv}", fields))
    return GLib.Variant("av", out)


def _pipelines_with_radius(pipelines: GLib.Variant, names: set[str], radius: float) -> GLib.Variant:
    out = {}
    for i in range(pipelines.n_children()):
        entry = pipelines.get_child_value(i)
        name = entry.get_child_value(0).get_string()
        body = entry.get_child_value(1)
        fields = {}
        for j in range(body.n_children()):
            field = body.get_child_value(j)
            key = field.get_child_value(0).get_string()
            value = field.get_child_value(1).get_variant()
            if name in names and key == "effects":
                value = _effects_with_radius(value, radius)
            fields[key] = value
        out[name] = fields
    return GLib.Variant("a{sa{sv}}", out)


def _radius_in(pipelines: GLib.Variant, names: set[str]) -> float | None:
    found = pipelines.unpack()
    for name in sorted(names):
        for effect in (found.get(name) or {}).get("effects", []):
            if effect.get("type") in BLUR_EFFECTS:
                params = effect.get("params") or {}
                value = params.get("unscaled_radius", params.get("radius"))
                if value is not None:
                    return float(value)
    return None


class Glass:
    """Blur behind the taskbar and the windows: on or off, how much, and
    how see-through the windows are. All of it is Blur My Shell's own
    settings for this account: the `blur` key of each part, the radius in
    the pipelines those parts use, and the windows' opacity."""

    def __init__(self, root=None, parts=None):
        if parts is None:
            parts = {p: _settings(f"org.gnome.shell.extensions.blur-my-shell.{p}", BLUR_SCHEMA_DIR)
                     for p in BLUR_PARTS}
            root = _settings("org.gnome.shell.extensions.blur-my-shell", BLUR_SCHEMA_DIR)
        self.root = root
        self.parts = {k: v for k, v in parts.items() if v is not None}

    @property
    def available(self) -> bool:
        return bool(self.parts)

    @property
    def blurred(self) -> bool:
        return any(p.get_boolean("blur") for p in self.parts.values())

    @blurred.setter
    def blurred(self, value: bool) -> None:
        for p in self.parts.values():
            p.set_boolean("blur", bool(value))

    def _pipelines(self) -> set[str]:
        return {p.get_string("pipeline") for p in self.parts.values()}

    @property
    def can_set_strength(self) -> bool:
        return self.root is not None and self.strength is not None

    @property
    def strength(self) -> float | None:
        """0..1 over RADIUS_RANGE."""
        if self.root is None:
            return None
        radius = _radius_in(self.root.get_value("pipelines"), self._pipelines())
        if radius is None:
            return None
        lo, hi = RADIUS_RANGE
        return max(0.0, min(1.0, (radius - lo) / (hi - lo)))

    @strength.setter
    def strength(self, value: float) -> None:
        lo, hi = RADIUS_RANGE
        radius = round(lo + (hi - lo) * max(0.0, min(1.0, value)), 1)
        self.root.set_value("pipelines", _pipelines_with_radius(
            self.root.get_value("pipelines"), self._pipelines(), radius))

    @property
    def can_set_transparency(self) -> bool:
        return "applications" in self.parts

    @property
    def transparency(self) -> float:
        """0 is a solid window, MAX_TRANSPARENCY the most see-through."""
        return 1.0 - self.parts["applications"].get_int("opacity") / 255

    @transparency.setter
    def transparency(self, value: float) -> None:
        value = max(0.0, min(MAX_TRANSPARENCY, value))
        self.parts["applications"].set_int("opacity", round(255 * (1.0 - value)))

    def reset(self) -> None:
        """Back to the image's defaults: the keys are unset, so the
        machine-wide values show through again."""
        for p in self.parts.values():
            p.reset("blur")
        if "applications" in self.parts:
            self.parts["applications"].reset("opacity")
        if self.root is not None:
            self.root.reset("pipelines")


# ---- the window ------------------------------------------------------------

class Window(Adw.ApplicationWindow):
    def __init__(self, app: Adw.Application | None, *, client=None, glass=None,
                 apply=apply_now, in_niri: bool | None = None):
        super().__init__(application=app, title="Look",
                         default_width=560, default_height=700)
        self.client = client
        self.glass = glass if glass is not None else Glass()
        self._quiet = False
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
        self.blur_row.connect("notify::active", self._on_blur_toggled)
        group.add(self.blur_row)

        self.strength = self.strength_row = None
        if g.can_set_strength:
            self.strength, self.strength_row = self._slider(
                TEXT["how_much"], TEXT["how_much_hint"], 0, 100, round(g.strength * 100),
                lambda v: setattr(g, "strength", v / 100))
            group.add(self.strength_row)
        self.transparency = self.transparency_row = None
        if g.can_set_transparency:
            self.transparency, self.transparency_row = self._slider(
                TEXT["transparency"], TEXT["transparency_hint"], 0, round(MAX_TRANSPARENCY * 100),
                round(g.transparency * 100), lambda v: setattr(g, "transparency", v / 100))
            group.add(self.transparency_row)

        self.reset_button = Gtk.Button(label=TEXT["reset"], halign=Gtk.Align.END, margin_top=12)
        self.reset_button.connect("clicked", lambda _b: self._reset_glass())
        group.add(self.reset_button)
        self._sliders_follow_switch()
        return group

    def _slider(self, title, hint, lo, hi, value, write):
        scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, lo, hi, 1)
        scale.set_hexpand(True)
        scale.set_size_request(200, -1)
        scale.set_draw_value(False)
        scale.set_value(value)
        scale.connect("value-changed", lambda s: self._quiet or write(s.get_value()))
        row = Adw.ActionRow(title=title, subtitle=hint)
        row.add_suffix(scale)
        return scale, row

    def _on_blur_toggled(self, row, _pspec) -> None:
        if not self._quiet:
            self.glass.blurred = row.get_active()
        self._sliders_follow_switch()

    def _sliders_follow_switch(self) -> None:
        on = self.blur_row.get_active()
        for row in (self.strength_row, self.transparency_row):
            if row is not None:
                row.set_sensitive(on)

    def _reset_glass(self) -> None:
        g = self.glass
        g.reset()
        # Show what is now in force without writing it back: the keys stay
        # unset, so the image's defaults keep applying.
        self._quiet = True
        try:
            self.blur_row.set_active(g.blurred)
            if self.strength is not None:
                self.strength.set_value(round(g.strength * 100))
            if self.transparency is not None:
                self.transparency.set_value(round(g.transparency * 100))
        finally:
            self._quiet = False
        self._sliders_follow_switch()

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
