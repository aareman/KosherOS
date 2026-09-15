"""Small pieces every page of the admin app uses.

Async plumbing (every daemon call runs off the main loop, because the
desktop polkit agent may prompt and block), the app's own CSS on top of
libadwaita, and the handful of widgets the design repeats: avatars, chips,
tags, a labelled status line.
"""

from __future__ import annotations

import re
import threading

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, GLib, Gtk  # noqa: E402

# On top of libadwaita, not instead of it: the banner that carries waiting
# requests is the accent colour (the user asked for "a blue notification
# at the top"), chips and tags reuse the platform's own colours so they
# hold up in light and dark, and cards get a pointer so they read as
# clickable.
CSS = b"""
.requests-banner > revealer > widget {
  background-color: @accent_bg_color;
  color: @accent_fg_color;
}
.requests-banner > revealer > widget label { color: @accent_fg_color; }
.requests-banner > revealer > widget button {
  background-color: alpha(@accent_fg_color, 0.2);
  color: @accent_fg_color;
}
.health-banner > revealer > widget {
  background-color: @warning_bg_color;
  color: @warning_fg_color;
}
.health-banner > revealer > widget label { color: @warning_fg_color; }
.chip {
  padding: 3px 10px;
  border-radius: 999px;
  background-color: alpha(currentColor, 0.08);
  font-size: 0.9em;
  font-weight: 600;
}
.chip.diff { background-color: alpha(@accent_bg_color, 0.18); color: @accent_color; }
.chip.dim { opacity: 0.6; }
/* What is being blocked reads blue; what is left open reads amber, so a
   parent can see at a glance which is which without reading the words. */
.chip.blocking { background-color: alpha(@accent_bg_color, 0.18); color: @accent_color; }
.chip.open { background-color: alpha(@warning_bg_color, 0.28); color: @warning_color; }
.line-open { color: @warning_color; }
.mode-badge {
  padding: 3px 12px;
  border-radius: 999px;
  font-weight: 700;
  background-color: @accent_bg_color;
  color: @accent_fg_color;
}
.mode-badge.open { background-color: @warning_bg_color; color: @warning_fg_color; }
.mode-badge.big { padding: 6px 18px; font-size: 1.15em; }
.tag {
  padding: 1px 7px;
  border-radius: 6px;
  background-color: alpha(currentColor, 0.08);
  font-size: 0.8em;
  font-weight: 600;
}
.tag.ok { background-color: alpha(@success_bg_color, 0.18); color: @success_color; }
.tag.warn { background-color: alpha(@warning_bg_color, 0.22); color: @warning_color; }
.tag.err { background-color: alpha(@error_bg_color, 0.16); color: @error_color; }
.tag.acc { background-color: alpha(@accent_bg_color, 0.18); color: @accent_color; }
.person-card { padding: 14px; }
.person-card:hover { background-color: alpha(currentColor, 0.04); }
.person-card.guest-off { border: 1px dashed alpha(currentColor, 0.25); background: none; }
.tile { padding: 12px; }
.tile:hover { background-color: alpha(currentColor, 0.04); }
.card-line { font-size: 0.92em; }
.time-label { font-variant-numeric: tabular-nums; min-width: 44px; }
.added-here { background-color: alpha(@accent_bg_color, 0.12); border-radius: 8px; }
.url { font-family: monospace; font-size: 0.85em; }
"""

_css_loaded = False


def load_css() -> None:
    global _css_loaded
    if _css_loaded:
        return
    display = Gdk.Display.get_default()
    if display is None:  # no display: tests that never draw
        return
    provider = Gtk.CssProvider()
    provider.load_from_data(CSS)
    Gtk.StyleContext.add_provider_for_display(
        display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
    _css_loaded = True


def run_async(work, on_done, on_error) -> None:
    """Run `work()` off the main loop; deliver result/exception on it."""

    def runner():
        try:
            result = work()
        except Exception as e:  # noqa: BLE001 - surfaced to the user as a toast
            GLib.idle_add(on_error, e)
        else:
            GLib.idle_add(on_done, result)

    threading.Thread(target=runner, daemon=True).start()


def error_text(e: Exception) -> str:
    msg = str(e)
    msg = re.sub(r"^.*?GDBus\.Error:[\w.]+: ", "", msg)
    return re.sub(r" \(\d+\)$", "", msg)


def clear(widget: Gtk.Widget) -> None:
    """Remove every child of a Box, ListBox or FlowBox."""
    while (child := widget.get_first_child()) is not None:
        widget.remove(child)


def avatar(name: str, size: int = 32) -> Adw.Avatar:
    return Adw.Avatar(size=size, text=name or "?", show_initials=True,
                      valign=Gtk.Align.CENTER)


def chip(text: str, *classes: str, icon: str | None = None) -> Gtk.Widget:
    box = Gtk.Box(spacing=5, valign=Gtk.Align.CENTER)
    box.add_css_class("chip")
    for c in classes:
        box.add_css_class(c)
    if icon:
        box.append(Gtk.Image(icon_name=icon, pixel_size=14))
    box.append(Gtk.Label(label=text))
    return box


def tag(text: str, *classes: str) -> Gtk.Label:
    label = Gtk.Label(label=text, valign=Gtk.Align.CENTER)
    label.add_css_class("tag")
    for c in classes:
        label.add_css_class(c)
    return label


def icon_line(icon_name: str, text: str, protects: bool | None = None) -> Gtk.Box:
    box = Gtk.Box(spacing=7)
    image = Gtk.Image(icon_name=icon_name, pixel_size=14)
    image.add_css_class("dim-label")
    box.append(image)
    label = Gtk.Label(label=text, xalign=0, ellipsize=3, hexpand=True,  # END
                      max_width_chars=30)
    label.add_css_class("card-line")
    if protects is False:
        # Not blocked: amber, so the one unprotected line on a card is the
        # one the eye goes to.
        label.add_css_class("line-open")
        image.remove_css_class("dim-label")
        image.add_css_class("line-open")
    box.append(label)
    return box


def mode_badge(text: str, protects: bool, big: bool = False) -> Gtk.Label:
    """The account's filter mode, as the loudest thing on the card."""
    label = Gtk.Label(label=text, halign=Gtk.Align.START, valign=Gtk.Align.CENTER)
    label.add_css_class("mode-badge")
    if not protects:
        label.add_css_class("open")
    if big:
        label.add_css_class("big")
    return label


def next_arrow() -> Gtk.Image:
    return Gtk.Image(icon_name="go-next-symbolic")


def small_button(label: str, *classes: str) -> Gtk.Button:
    button = Gtk.Button(label=label, valign=Gtk.Align.CENTER)
    for c in classes:
        button.add_css_class(c)
    return button


CLICKABLE = ("Gtk.Button", "Gtk.MenuButton", "Gtk.Switch", "Gtk.CheckButton",
             "Gtk.ToggleButton", "Gtk.LinkButton", "Gtk.FlowBoxChild", "Adw.ButtonRow",
             "Adw.ComboRow", "Adw.SwitchRow", "Adw.ExpanderRow")


def pointer_cursors(root: Gtk.Widget) -> int:
    """Give everything clickable under `root` the hand cursor, like the web.

    GTK's convention is an arrow everywhere; the user asked for the cursor
    to say what can be clicked. GTK CSS has no `cursor` property, so it is
    set on the widgets: buttons, switches, checks, combos, list rows that
    act, and the cards and tiles on the board. Rows that only display keep
    the arrow. Returns how many widgets were marked.
    """
    marked = 0
    child = root.get_first_child()
    while child is not None:
        if _clickable(child):
            child.set_cursor_from_name("pointer")
            marked += 1
        marked += pointer_cursors(child)
        child = child.get_next_sibling()
    return marked


def _clickable(widget: Gtk.Widget) -> bool:
    if isinstance(widget, Adw.ActionRow):
        return bool(widget.get_activatable()) and not isinstance(widget, Adw.EntryRow)
    if isinstance(widget, Gtk.ListBoxRow) and not isinstance(widget, Adw.PreferencesRow):
        # The people rail on the activity tab, and any plain list of choices.
        return widget.get_activatable() or widget.get_selectable()
    name = type(widget).__name__
    module = type(widget).__module__.split(".")[-1]
    return f"{module}.{name}" in CLICKABLE or (
        isinstance(widget, (Gtk.Button, Gtk.Switch, Gtk.CheckButton, Gtk.FlowBoxChild)))


def confirm(parent, heading: str, body: str, verb: str, on_yes,
            destructive: bool = True) -> Adw.AlertDialog:
    dialog = Adw.AlertDialog(heading=heading, body=body)
    dialog.add_response("cancel", "Cancel")
    dialog.add_response("yes", verb)
    dialog.set_response_appearance(
        "yes", Adw.ResponseAppearance.DESTRUCTIVE if destructive
        else Adw.ResponseAppearance.SUGGESTED)

    def on_response(_d, response):
        if response == "yes":
            on_yes()

    dialog.connect("response", on_response)
    dialog.present(parent)
    return dialog
