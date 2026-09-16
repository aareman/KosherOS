"""The sidebar: where everything in the app is, in one column.

Two groups. **Family** first — a row to add a person, then one row per
account, the guest last — because the people are what an admin came to
configure. Then **Administration**: the activity feed, what protects
everyone, what may be installed, what version runs.

There is no overview page any more. The board of cards duplicated this
list, and the family said so; the sidebar row and the person's own page
are the two things that remain. The waiting-requests and filter-health
banners that used to sit on the board sit above every page now.

Every row carries its own state on the right — which preset, whether
anyone is waiting, whether the filter is running — so the sidebar answers
most of "is anything wrong?" without being clicked.
"""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, Gtk  # noqa: E402

from kosherd import profiles as profiles_mod  # noqa: E402

from . import labels  # noqa: E402
from .common import avatar, clear, tag  # noqa: E402
from .computer import health_summary  # noqa: E402

# The rows that are always there, below the family.
ADMIN = (
    ("activity", "Activity", "document-open-recent-symbolic"),
    ("protection", "Protection", "security-high-symbolic"),
    ("apps", "Apps", "view-grid-symbolic"),
    ("updates", "Updates", "software-update-available-symbolic"),
)
FAMILY_SECTION = "Family"
ADMIN_SECTION = "Administration"
ADD_KEY = "add"

# Kept for anything that wants the fixed destinations in one tuple.
DESTINATIONS = ADMIN


def user_key(user: dict) -> str:
    """The sidebar key for an account. The guest keeps one key whether it
    is on or off, so turning it on does not move the selection."""
    return "guest" if user.get("guest") else f"user-{user['uid']}"


class _Row(Gtk.ListBoxRow):
    """One destination: something on the left, a word or two on the right."""

    def __init__(self, key: str, title: str, section: str | None):
        super().__init__()
        self.key = key
        self.section = section
        self.box = Gtk.Box(spacing=12, margin_top=6, margin_bottom=6,
                           margin_start=6, margin_end=6)
        self.title = Gtk.Label(label=title, xalign=0, hexpand=True, ellipsize=3)
        self.status = Gtk.Label(xalign=1, visible=False)
        self.status.add_css_class("dim-label")
        self.status.add_css_class("caption")
        self.badge = tag("", "acc")
        self.badge.set_visible(False)
        self.set_child(self.box)
        self.set_cursor_from_name("pointer")

    def _pack(self, lead: Gtk.Widget) -> None:
        self.box.append(lead)
        self.box.append(self.title)
        self.box.append(self.status)
        self.box.append(self.badge)

    def say(self, text: str = "", badge: str = "", badge_class: str = "acc") -> None:
        self.status.set_label(text)
        self.status.set_visible(bool(text))
        if badge:
            for name in ("acc", "warn", "err", "ok"):
                self.badge.remove_css_class(name)
            self.badge.add_css_class(badge_class)
            self.badge.set_label(badge)
        self.badge.set_visible(bool(badge))


class SidebarRow(_Row):
    def __init__(self, key: str, title: str, icon: str, section: str | None = None):
        super().__init__(key, title, section)
        self._pack(Gtk.Image(icon_name=icon, pixel_size=16))


class PersonRow(_Row):
    """An account, with its face and the preset it is set up as."""

    def __init__(self, user: dict, waiting: int, custom=()):
        super().__init__(user_key(user), user["username"], FAMILY_SECTION)
        self.uid = user["uid"]
        self._pack(avatar(user["username"], 22))
        if user.get("guest"):
            self.say("On" if user.get("enabled", True) else "Off")
            return
        key, _changes = profiles_mod.diff(user, custom)
        # The preset only, never "Child, with 2 changes": the drift belongs
        # on the person's own page, and a sidebar that wraps is no sidebar.
        preset = profiles_mod.get(key, custom).label if key else "Custom"
        self.say(preset, badge=str(waiting) if waiting else "")


class Sidebar(Adw.NavigationPage):
    """The navigation pane of the split view."""

    def __init__(self, win):
        super().__init__(title="KosherOS Admin", tag="sidebar")
        self.win = win
        self.rows: dict[str, _Row] = {}
        self._selecting = False
        self._add_popover: Gtk.PopoverMenu | None = None

        self.list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        self.list.add_css_class("navigation-sidebar")
        self.list.set_header_func(self._header)
        self.list.connect("row-selected", self._on_selected)

        scroller = Gtk.ScrolledWindow(vexpand=True,
                                      hscrollbar_policy=Gtk.PolicyType.NEVER)
        scroller.set_child(self.list)

        header = Adw.HeaderBar()
        header.set_title_widget(Adw.WindowTitle(title="KosherOS", subtitle="Admin"))
        # The two ways to add somebody, offered from the "Add a person…" row
        # at the top of the family (a bare + in the header was not clear).
        self.add_menu = Gio.Menu()
        self.add_menu.append("Create User Account…", "win.create-user")
        self.add_menu.append("Adopt Existing User…", "win.adopt-user")
        lock = Gtk.Button(icon_name="changes-prevent-symbolic",
                          tooltip_text="Lock now")
        lock.connect("clicked", lambda _b: win.lock())
        header.pack_end(lock)

        view = Adw.ToolbarView()
        view.add_top_bar(header)
        view.set_content(scroller)
        self.set_child(view)
        self.refresh()

    @staticmethod
    def _header(row, before) -> None:
        section = getattr(row, "section", None)
        if section is None or (before is not None
                               and getattr(before, "section", None) == section):
            row.set_header(None)
            return
        label = Gtk.Label(label=section.upper(), xalign=0, margin_start=14,
                          margin_top=14, margin_bottom=2)
        label.add_css_class("caption-heading")
        label.add_css_class("dim-label")
        row.set_header(label)

    # -- selection -----------------------------------------------------------------

    def select(self, key: str) -> None:
        """Move the selection without acting on it (the window already has)."""
        row = self.rows.get(key)
        self._selecting = True
        if row is None:
            self.list.unselect_all()
        elif not row.is_selected():
            self.list.select_row(row)
        self._selecting = False

    def _on_selected(self, _list, row) -> None:
        if row is None or self._selecting:
            return
        if row.key == ADD_KEY:
            # A choice, not a destination: offer the two ways to add a person
            # and leave the selection where it was.
            self.offer_add(row)
            self.select(getattr(self.win, "destination", ""))
            return
        self.win.go_to(row.key)

    def offer_add(self, row) -> Gtk.PopoverMenu:
        """The two ways to add a person, popped up beside their row.

        One popover for the sidebar's lifetime, parented to the list (which
        outlives the rows, rebuilt on every refresh) and pointed at the row
        when asked: a popover parented to a row that is later disposed is
        how GTK4 crashes. Not popped up at all when the list is in no window
        yet — the tests build the sidebar bare.
        """
        if self._add_popover is None:
            self._add_popover = Gtk.PopoverMenu.new_from_model(self.add_menu)
            self._add_popover.set_parent(self.list)
            self._add_popover.set_has_arrow(True)
        ok, bounds = row.compute_bounds(self.list)
        if ok:
            rect = Gdk.Rectangle()
            rect.x, rect.y = int(bounds.origin.x), int(bounds.origin.y)
            rect.width, rect.height = int(bounds.size.width), int(bounds.size.height)
            self._add_popover.set_pointing_to(rect)
        if self.list.get_root() is not None:
            self._add_popover.popup()
        return self._add_popover

    # -- what the rows say ----------------------------------------------------------

    def refresh(self) -> None:
        """Rebuild from what the window last loaded.

        The people come and go, so the list is rebuilt rather than patched;
        it is a dozen rows. Selection is restored afterwards from the
        window's current destination, with the signal muted throughout so
        rebuilding never looks like a click.
        """
        win = self.win
        policy = getattr(win, "policy", None) or {}
        self._selecting = True
        clear(self.list)
        self.rows = {}

        add = SidebarRow(ADD_KEY, "Add a person…", "list-add-symbolic", FAMILY_SECTION)
        add.title.add_css_class("accent")
        self._add(add)

        waiting: dict[int, int] = {}
        for request in getattr(win, "requests", None) or []:
            waiting[request["uid"]] = waiting.get(request["uid"], 0) + 1
        custom = policy.get("custom_profiles", [])
        for user in policy.get("users", []):
            self._add(PersonRow(user, waiting.get(user["uid"], 0), custom))
        guest = dict(policy.get("guest") or {"enabled": False})
        guest.update(guest=True, username="Guest", uid=guest.get("uid", -2))
        self._add(PersonRow(guest, 0))

        for key, title, icon in ADMIN:
            self._add(SidebarRow(key, title, icon, ADMIN_SECTION))

        self._say_computer()
        self._selecting = False
        self.select(getattr(win, "destination", ""))

    def _add(self, row: _Row) -> None:
        self.rows[row.key] = row
        self.list.append(row)

    def _say_computer(self) -> None:
        win = self.win
        blocked = sum(counts.get("blocked", 0)
                      for counts in (getattr(win, "summary", None) or {}).values()
                      if isinstance(counts, dict))
        self.rows["activity"].say(f"{blocked} blocked today" if blocked
                                  else "Quiet today")

        words, ok = health_summary(getattr(win, "status", None))
        self.rows["protection"].say("" if not ok else words,
                                    badge="" if ok else words, badge_class="warn")

        approved = getattr(win, "catalog_count", None)
        self.rows["apps"].say(labels.plural(approved, "app")
                              if approved is not None else "")

        # Until somebody checks, the useful thing to say is what this
        # computer is running; an empty row looks unfinished.
        booted = (getattr(win, "deployment", None) or {}).get("booted") or {}
        state = getattr(win, "update_state", None) or (
            f"Version {booted['version']}" if booted.get("version") else "")
        self.rows["updates"].say(state[:24])
