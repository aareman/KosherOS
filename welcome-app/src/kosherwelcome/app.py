"""Welcome — what this computer is and where things are, once per account.

GNOME's own welcome dialog explains GNOME and offers a tour of it. This
one explains the computer the person is sitting at, and it is a different
window for an administrator and for a managed user: one has a family to
set up, the other has an account that is filtered and wants to know where
their apps are and what to do about a blocked page.

It runs from an autostart entry at every GNOME sign-in and leaves at once
when this account has seen it already. What it says comes from the same
GetMySettings call My Filter makes, so an admin is told admin things and
a person with a time limit is told what it is.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import threading
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

from kosherd.client import DaemonClient  # noqa: E402

APP_ID = "org.kosherlinux.Welcome"
LOGO = "/usr/share/pixmaps/kosheros-logo.png"
# Seen once per account; the stamp lives beside the sign-in helper's.
STAMP = Path(".config/kosher/welcomed")


def seen(home: Path | None = None) -> bool:
    return ((home or Path.home()) / STAMP).exists()


def mark_seen(home: Path | None = None) -> None:
    target = (home or Path.home()) / STAMP
    target.parent.mkdir(parents=True, exist_ok=True)
    target.touch()


def first_name(settings: dict) -> str:
    """The person's first name from the account's full name, or the
    username when there is none: "Welcome, Yosef" rather than "Welcome,
    yosef1".
    """
    real = GLib.get_real_name() or ""
    if real and real != "Unknown":
        return real.split()[0]
    return settings.get("username") or ""


# ---- what is said -----------------------------------------------------------
#
# Plain sentences that say one concrete thing and stop. Each page is a
# title, a paragraph, and rows of (icon, name, what it is).

ADMIN_PAGES = (
    ("Welcome to KosherOS, {name}",
     "This computer is set up for a family. The internet is filtered for each "
     "account separately, and you are an administrator, so you decide what each "
     "person gets.",
     ()),
    ("Where things are",
     "",
     (("go-home-symbolic", "Apps",
       "The button at the bottom left opens every program on this computer."),
      ("security-high-symbolic", "KosherOS Admin",
       "Family accounts, what each one may see, time limits, and what the filter has been doing."),
      ("system-software-install-symbolic", "Store",
       "Install apps. Each account gets the ones you have approved for it."),
      ("preferences-desktop-appearance-symbolic", "Look",
       "Each person chooses their own desktop and how the taskbar looks."),
      ("view-list-bullet-symbolic", "My Filter",
       "Shows an account its own rules in plain words."))),
    ("A few things to do first",
     "",
     (("system-users-symbolic", "Add an account for each person",
       "In KosherOS Admin. Each one has its own filter settings."),
      ("dialog-password-symbolic", "Set a guardian password if you want one",
       "Then changes to the filtering ask for a second password, even from an administrator."),
      ("software-update-available-symbolic", "Updates arrive on their own",
       "KosherOS Admin shows when one is waiting to be installed."),
      ("input-keyboard-symbolic", "Hebrew",
       "Alt+Shift switches the keyboard between English and Hebrew."))),
)

USER_PAGES = (
    ("Welcome, {name}",
     "This computer belongs to a family, and the internet on your account is "
     "filtered. Some sites and pictures will not open. That is the filter working, "
     "not something broken.",
     ()),
    ("Where things are",
     "",
     (("go-home-symbolic", "Apps",
       "The button at the bottom left opens every program on this computer."),
      ("system-software-install-symbolic", "Store",
       "The apps you are allowed to install."),
      ("preferences-desktop-appearance-symbolic", "Look",
       "Choose your desktop and how the taskbar looks."),
      ("view-list-bullet-symbolic", "My Filter",
       "Your own rules in plain words, and how much time you have today."))),
    ("Good to know",
     "",
     (("mail-send-symbolic", "A page you need is blocked?",
       "The blocked page has an \"Ask for this page\" button. An administrator sees the request."),
      ("input-keyboard-symbolic", "Hebrew",
       "Alt+Shift switches the keyboard between English and Hebrew."))),
)

TIME_ROW = ("alarm-symbolic", "Your time",
            "You have {limit} on this computer each day. My Filter shows what is left.")

BUTTONS = {"next": "Next", "back": "Back", "done": "Done", "admin": "Open KosherOS Admin"}


def pages_for(settings: dict) -> list[tuple[str, str, tuple]]:
    """The pages for this person: admin or user, with the time limit row
    added for a user who has one."""
    name = first_name(settings)
    if settings.get("admin"):
        pages = ADMIN_PAGES
    else:
        pages = USER_PAGES
    out = [(title.format(name=name), body, tuple(rows)) for title, body, rows in pages]
    t = settings.get("time") or {}
    if not settings.get("admin") and t.get("limited") and t.get("daily_minutes"):
        from kosherd import timelimits

        limit = timelimits.duration_text(int(t["daily_minutes"]) * 60)
        icon, title, words = TIME_ROW
        last_title, last_body, last_rows = out[-1]
        out[-1] = (last_title, last_body, last_rows + ((icon, title, words.format(limit=limit)),))
    return out


def _run_async(work, on_done, on_error) -> None:
    def runner():
        try:
            result = work()
        except Exception as e:  # noqa: BLE001
            GLib.idle_add(on_error, e)
        else:
            GLib.idle_add(on_done, result)

    threading.Thread(target=runner, daemon=True).start()


def open_admin() -> None:
    subprocess.Popen(["kosher-admin"])


# ---- the window ------------------------------------------------------------

class Window(Adw.ApplicationWindow):
    def __init__(self, app: Adw.Application | None, *, client=None, home: Path | None = None):
        super().__init__(application=app, title="Welcome to KosherOS",
                         default_width=620, default_height=600)
        self.client = client
        self.home = home
        self.pages: list = []
        self.view = Adw.ToolbarView()
        self.set_content(self.view)
        self.view.add_top_bar(Adw.HeaderBar(show_title=False))
        self.view.set_content(Adw.StatusPage(
            title="", child=Gtk.Spinner(spinning=True, width_request=32, height_request=32)))
        # Seen is seen: closing the window counts, so it never nags.
        self.connect("close-request", lambda _w: (mark_seen(self.home), False)[1])
        _run_async(lambda: (self.client or DaemonClient()).get_my_settings(),
                   self._on_loaded, self._on_error)

    def _on_error(self, _e: Exception) -> None:
        # Without the daemon there is nothing true to say about this account;
        # say the general thing rather than nothing.
        self._on_loaded({"managed": False, "admin": False, "username": ""})

    def _on_loaded(self, settings: dict) -> None:
        self.settings = settings
        self.pages = pages_for(settings)
        self.carousel = Adw.Carousel(hexpand=True, vexpand=True, interactive=True,
                                     allow_scroll_wheel=False)
        for i, (title, body, rows) in enumerate(self.pages):
            self.carousel.append(self._page(title, body, rows, logo=(i == 0)))
        dots = Adw.CarouselIndicatorDots(carousel=self.carousel, margin_bottom=6)

        self.back = Gtk.Button(label=BUTTONS["back"])
        self.back.connect("clicked", lambda _b: self._go(-1))
        self.next = Gtk.Button(label=BUTTONS["next"])
        self.next.add_css_class("suggested-action")
        self.next.connect("clicked", lambda _b: self._go(+1))
        self.admin_button = Gtk.Button(label=BUTTONS["admin"], visible=False)
        self.admin_button.connect("clicked", lambda _b: (open_admin(), self._finish()))
        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12,
                      margin_start=24, margin_end=24, margin_bottom=24)
        bar.append(self.back)
        spacer = Gtk.Box(hexpand=True)
        bar.append(spacer)
        bar.append(self.admin_button)
        bar.append(self.next)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.append(self.carousel)
        box.append(dots)
        box.append(bar)
        self.view.set_content(box)
        # The page the buttons act on. Kept here rather than read back from
        # the carousel, whose animated position lags the click; a swipe
        # reports in through page-changed.
        self.index = 0
        self.carousel.connect("page-changed", self._on_page_changed)
        self._buttons()
        self.next.grab_focus()

    def _on_page_changed(self, _carousel, index: int) -> None:
        # An unmapped carousel cannot scroll and reports page 0 for any
        # request; only a shown window's report (a swipe, or the end of a
        # button's scroll) is the truth.
        if not self.get_mapped():
            return
        self.index = int(index)
        self._buttons()

    def _page(self, title: str, body: str, rows: tuple, *, logo: bool) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                      margin_top=12, margin_start=36, margin_end=36, valign=Gtk.Align.CENTER)
        if logo and os.path.exists(LOGO):
            image = Gtk.Image.new_from_file(LOGO)
            image.set_pixel_size(96)
            box.append(image)
        heading = Gtk.Label(label=title, wrap=True, justify=Gtk.Justification.CENTER,
                            css_classes=["title-1"])
        box.append(heading)
        if body:
            box.append(Gtk.Label(label=body, wrap=True, justify=Gtk.Justification.CENTER,
                                 css_classes=["body"], max_width_chars=60))
        if rows:
            listbox = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, margin_top=12)
            listbox.add_css_class("boxed-list")
            for icon, name, words in rows:
                row = Adw.ActionRow(title=name, subtitle=words, activatable=False)
                row.add_prefix(Gtk.Image(icon_name=icon, pixel_size=24))
                listbox.append(row)
            box.append(listbox)
        return box

    def _go(self, step: int) -> None:
        i = self.index + step
        if i >= len(self.pages):
            self._finish()
            return
        self.index = max(0, i)
        # No animation while unmapped (tests, a window not yet shown): the
        # jump is immediate and page-changed reports the right page.
        self.carousel.scroll_to(self.carousel.get_nth_page(self.index), self.get_mapped())
        self._buttons()

    def _buttons(self) -> None:
        i = self.index
        last = i == len(self.pages) - 1
        self.back.set_visible(i > 0)
        self.next.set_label(BUTTONS["done"] if last else BUTTONS["next"])
        self.admin_button.set_visible(last and bool(self.settings.get("admin")))

    def _finish(self) -> None:
        mark_seen(self.home)
        self.close()


class Application(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID)

    def do_activate(self):
        window = self.props.active_window or Window(self)
        window.present()


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="the KosherOS welcome, once per account")
    p.add_argument("--again", action="store_true", help="open it even if this account has seen it")
    args = p.parse_args(argv)
    if seen() and not args.again:
        return 0
    return Application().run(None)


if __name__ == "__main__":
    sys.exit(main())
