"""The sidebar: where everything in the app is, in one column.

Two sections, because there are two kinds of thing to set here. "The
family" is per person — the board and the diary. "This computer" is the
rest: what protects everyone, what may be installed, what version runs.
They used to be a row of five small tiles at the bottom of the family
board, below the fold, which made the whole machine look like an
afterthought next to the people.

Each row carries its own state on the right — how many people, how many
apps, whether the filter is running — so the sidebar answers most of
"is anything wrong?" without being clicked.
"""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk  # noqa: E402

from . import labels  # noqa: E402
from .common import tag  # noqa: E402
from .computer import health_summary  # noqa: E402

# key, title, icon, section heading above it (None: same section as above)
DESTINATIONS = (
    ("family", "Family", "system-users-symbolic", "The family"),
    ("activity", "Activity", "document-open-recent-symbolic", None),
    ("protection", "Protection", "security-high-symbolic", "This computer"),
    ("apps", "Apps", "view-grid-symbolic", None),
    ("updates", "Updates", "software-update-available-symbolic", None),
)


class SidebarRow(Gtk.ListBoxRow):
    def __init__(self, key: str, title: str, icon: str):
        super().__init__()
        self.key = key
        box = Gtk.Box(spacing=12, margin_top=8, margin_bottom=8,
                      margin_start=6, margin_end=6)
        box.append(Gtk.Image(icon_name=icon, pixel_size=16))
        box.append(Gtk.Label(label=title, xalign=0, hexpand=True, ellipsize=3))
        self.status = Gtk.Label(xalign=1, visible=False)
        self.status.add_css_class("dim-label")
        self.status.add_css_class("caption")
        box.append(self.status)
        self.badge = tag("", "acc")
        self.badge.set_visible(False)
        box.append(self.badge)
        self.set_child(box)
        self.set_cursor_from_name("pointer")

    def say(self, text: str = "", badge: str = "", badge_class: str = "acc") -> None:
        self.status.set_label(text)
        self.status.set_visible(bool(text))
        if badge:
            for name in ("acc", "warn", "err", "ok"):
                self.badge.remove_css_class(name)
            self.badge.add_css_class(badge_class)
            self.badge.set_label(badge)
        self.badge.set_visible(bool(badge))


class Sidebar(Adw.NavigationPage):
    """The navigation pane of the split view."""

    def __init__(self, win):
        super().__init__(title="KosherOS Admin", tag="sidebar")
        self.win = win
        self.rows: dict[str, SidebarRow] = {}
        self._selecting = False

        self.list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        self.list.add_css_class("navigation-sidebar")
        for key, title, icon, section in DESTINATIONS:
            row = SidebarRow(key, title, icon)
            row.section = section
            self.rows[key] = row
            self.list.append(row)
        self.list.set_header_func(self._header)
        self.list.connect("row-selected", self._on_selected)

        scroller = Gtk.ScrolledWindow(vexpand=True,
                                      hscrollbar_policy=Gtk.PolicyType.NEVER)
        scroller.set_child(self.list)

        header = Adw.HeaderBar()
        header.set_title_widget(Adw.WindowTitle(title="KosherOS", subtitle="Admin"))
        lock = Gtk.Button(icon_name="changes-prevent-symbolic",
                          tooltip_text="Lock now")
        lock.connect("clicked", lambda _b: win.lock())
        header.pack_end(lock)

        view = Adw.ToolbarView()
        view.add_top_bar(header)
        view.set_content(scroller)
        self.set_child(view)

    @staticmethod
    def _header(row, before) -> None:
        section = getattr(row, "section", None)
        if section is None or (before is not None
                               and getattr(before, "section", None) == section):
            row.set_header(None)
            return
        label = Gtk.Label(label=section.upper(), xalign=0, margin_start=14,
                          margin_top=14 if before is not None else 8,
                          margin_bottom=2)
        label.add_css_class("caption-heading")
        label.add_css_class("dim-label")
        row.set_header(label)

    def select(self, key: str) -> None:
        """Move the selection without acting on it (the window already has)."""
        row = self.rows.get(key)
        if row is None or row.is_selected():
            return
        self._selecting = True
        self.list.select_row(row)
        self._selecting = False

    def _on_selected(self, _list, row) -> None:
        if row is None or self._selecting:
            return
        self.win.go_to(row.key)

    def refresh(self) -> None:
        """Everything the rows say, from what the window last loaded."""
        win = self.win
        people = len(win.policy.get("users", []))
        waiting = len(win.requests or [])
        self.rows["family"].say(
            labels.plural(people, "person", "people") if people else "Nobody yet",
            badge=str(waiting) if waiting else "")

        blocked = sum(counts.get("blocked", 0)
                      for counts in (win.summary or {}).values()
                      if isinstance(counts, dict))
        self.rows["activity"].say(f"{blocked} blocked today" if blocked else "Quiet today")

        words, ok = health_summary(win.status)
        self.rows["protection"].say("" if not ok else words,
                                    badge="" if ok else words,
                                    badge_class="warn")

        approved = win.catalog_count
        self.rows["apps"].say(labels.plural(approved, "app") if approved is not None else "")

        state = win.update_state or ""
        self.rows["updates"].say(state[:22])
