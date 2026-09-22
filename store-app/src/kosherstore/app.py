"""KosherOS Store — browse and install applications.

Open to every user. What it shows is decided by kosherd for the account
at the keyboard (kosherd.appaccess): the apps an administrator has
approved, or — for an account opened to the whole store — everything on
Flathub minus the kinds and apps blocked for it and minus anything rated
above the computer's content ceiling. So anything listed here is already
something this account may have. Installs are performed by kosherd (this
app has no privileges of its own) and progress arrives over D-Bus.

It looks like a store — it opens on the categories, a search box is always
in the header, and choosing a category (or searching) moves the categories
into a sidebar with the apps beside them — and it opens quickly, because
everything it draws is local: the list is one D-Bus reply from an index
kosherd already holds, and each icon is a file flatpak already downloaded
beside the remote's catalogue. Nothing here parses the 40 MB app index or
touches the network, and a shelf of thousands is drawn a page at a time.
"""

from __future__ import annotations

import threading

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Pango", "1.0")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk, Pango  # noqa: E402

from kosherd import appkinds  # noqa: E402
from kosherd.client import DaemonClient  # noqa: E402

APP_ID = "org.kosherlinux.Store"

# The shelves are the kinds of app kosherd blocks by (kosherd.appkinds):
# one vocabulary, so what the admin app calls Games is exactly this shelf.
SHELVES = appkinds.KINDS
ICON_SIZE = 48
FALLBACK_ICON = "application-x-executable"
# When an app has no icon on this machine at all — a fresh install whose
# flatpak metadata has not been fetched yet, or an app whose publisher
# shipped none — draw a lettered tile in its category's colour rather than
# the same grey box forty times. It is distinct per app, instant, and looks
# deliberate instead of broken.
SHELF_TINTS = appkinds.KIND_TINTS
SHELF_ICONS = appkinds.KIND_ICONS
# How many cards a shelf draws before offering the rest. The whole store
# is thousands of apps; a grid of thousands of widgets takes seconds on
# the hardware this runs on, and nobody reads past the first screens
# without searching anyway.
PAGE_SIZE = 80

def _tint_css() -> bytes:
    rules = [".app-letter { border-radius: 12px; font-weight: 800; color: #ffffff; }"]
    for shelf, colour in SHELF_TINTS.items():
        rules.append(f".app-letter.{shelf} {{ background-color: {colour}; }}")
    return "\n".join(rules).encode()


CSS = b"""
.app-card { padding: 12px 14px; }
.app-card button.pill { min-height: 26px; padding: 2px 16px; font-size: 0.92em; }
.app-card:hover { background-color: alpha(currentColor, 0.04); }
.shelf-tile { padding: 8px; }
.app-letter { border-radius: 12px; }
.shelf-tile:hover { background-color: alpha(currentColor, 0.05); }
"""


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


def shelf_of(app: dict) -> str:
    """Which shelf an app belongs on, from the categories its publisher set."""
    return appkinds.kind_of(app)


def icon_candidates(app: dict) -> list[str]:
    """Themed icon names to try for an app, best first.

    Its own id (an installed flatpak exports its icon under that name), any
    name the catalogue carries, then the icon for the shelf it sits on — so
    a browser without an icon still looks like a browser, rather than
    twenty identical grey boxes down the page.
    """
    names = [app["ref"], app.get("icon_name") or "",
             app["ref"].rsplit(".", 1)[-1].lower()]
    names += list(SHELF_ICONS.get(shelf_of(app), ()))
    names.append(FALLBACK_ICON)
    return [n for n in dict.fromkeys(names) if n]


def letter_tile(app: dict, size: int = ICON_SIZE) -> Gtk.Label:
    """The app's initial on a tile in its category's colour.

    What is drawn when the machine has no icon for an app: distinct per
    app, and it reads as a choice rather than a missing file.
    """
    name = (app.get("name") or app["ref"].rsplit(".", 1)[-1]).strip()
    letter = next((c for c in name if c.isalnum()), "?").upper()
    label = Gtk.Label(label=letter, width_request=size, height_request=size,
                      valign=Gtk.Align.CENTER, halign=Gtk.Align.CENTER)
    label.add_css_class("app-letter")
    label.add_css_class(shelf_of(app))
    # Sized to the tile rather than to the theme, so it matches a real icon.
    attrs = Pango.AttrList()
    attrs.insert(Pango.attr_scale_new(size / 24))
    label.set_attributes(attrs)
    return label


def app_icon(app: dict, size: int = ICON_SIZE) -> Gtk.Widget:
    """The app's icon: the file flatpak cached or the app exported, else a
    themed icon, else a lettered tile. Never a network fetch, never an
    error, and never an empty space."""
    from kosherd import apps as apps_mod

    try:
        path = apps_mod.icon_file(app, size)
    except Exception:  # noqa: BLE001 - an icon is never worth a failure
        path = ""
    if path:
        return Gtk.Image(pixel_size=size, file=path)
    display = Gdk.Display.get_default()
    theme = Gtk.IconTheme.get_for_display(display) if display is not None else None
    if theme is not None:
        for name in icon_candidates(app):
            if name != FALLBACK_ICON and theme.has_icon(name):
                return Gtk.Image(pixel_size=size, icon_name=name)
    return letter_tile(app, size)


class AppCard(Gtk.Box):
    """One app: icon, name, what it is, and the button that acts on it."""

    def __init__(self, app: dict, store: "Window"):
        # A fixed height as well as a fixed width: a summary that wraps to
        # two lines on a narrow card and one on a wide one changed the row
        # height, which changed whether the list needed a scrollbar, which
        # changed the width — the loop a person sees as flickering.
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6,
                         width_request=280, height_request=96)
        self.app = app
        self.ref = app["ref"]
        self.store = store
        self.add_css_class("card")
        self.add_css_class("app-card")

        top = Gtk.Box(spacing=12)
        top.append(app_icon(app))
        names = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, valign=Gtk.Align.CENTER,
                        hexpand=True)
        title = Gtk.Label(label=app.get("name", self.ref), xalign=0, ellipsize=3,
                          max_width_chars=18)
        title.add_css_class("heading")
        names.append(title)
        subtitle = Gtk.Label(label=app.get("summary") or self.ref, xalign=0, wrap=True,
                             lines=2, ellipsize=3, max_width_chars=24,
                             valign=Gtk.Align.START)
        subtitle.add_css_class("dim-label")
        subtitle.add_css_class("caption")
        names.append(subtitle)
        top.append(names)

        # A small, quiet button at the end of the row. It used to be a
        # full-width blue bar on every card, which shouted louder than the
        # apps themselves; in a store the app is the thing being looked at
        # and the button is what you reach for once you have chosen.
        self.button = Gtk.Button(valign=Gtk.Align.CENTER)
        self.button.add_css_class("pill")
        self.button.connect("clicked", self._on_clicked)
        self.button.set_cursor_from_name("pointer")
        top.append(self.button)
        self.append(top)

        self.progress = Gtk.ProgressBar(show_text=True, visible=False,
                                        valign=Gtk.Align.CENTER)
        self.append(self.progress)
        # The store remembers what is in hand, so a card built while an
        # install is running shows the progress rather than an Install
        # button that would start it a second time.
        percent, status = store.work_of(self.ref)
        self.set_state(store.state_of(self.ref), percent, status)

    def set_state(self, state: str, percent: int = 0, status: str = "") -> None:
        self.state = state
        busy = state == "working"
        self.progress.set_visible(busy)
        self.button.set_visible(not busy)
        if busy:
            self.progress.set_fraction(percent / 100)
            self.progress.set_text(status or f"{percent}%")
            return
        if state == "update":
            # Installed, and a newer build is on the remote. The one button
            # on the card is the update; removing is still on the Installed
            # shelf, where the app is not asking for anything.
            self.button.set_label("Update")
            self.button.set_css_classes(["pill", "suggested-action"])
            self.button.set_sensitive(True)
            self.button.set_tooltip_text(self.store.update_words(self.ref))
            return
        if state == "installed":
            self.button.set_label("Remove")
            self.button.set_css_classes(["pill", "flat"])
            self.button.set_sensitive(self.store.is_admin)
            self.button.set_tooltip_text(
                None if self.store.is_admin else "Only an admin can remove apps")
        else:
            self.button.set_label("Install")
            self.button.set_css_classes(["pill"])
            self.button.set_sensitive(self.store.can_install)
            self.button.set_tooltip_text(
                None if self.store.can_install else
                "App installation is turned off for your account")

    def _on_clicked(self, _b) -> None:
        before = self.state
        if before == "working":
            return  # already in hand: a second request is an error, not a wish
        # Several apps can be requested at once; kosherd queues them.
        self.store.work_started(self.ref)
        self.set_state("working", 0, "Queued…")
        work = {"installed": self.store.client.remove_app,
                "update": self.store.client.update_app}.get(
                    before, self.store.client.install_app)

        def on_error(e):
            self.store.work_ended(self.ref)
            self.set_state(before)
            self.store.toast(_error_text(e))

        _run_async(lambda: work(self.ref), lambda _r: None, on_error)


class ShelfTile(Gtk.Box):
    """One category on the home page: what it is, and how much is on it."""

    def __init__(self, key: str, label: str, count: int, icon: str, words: str = ""):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=8,
                         width_request=190, height_request=120)
        self.key = key
        self.add_css_class("card")
        self.add_css_class("shelf-tile")
        image = Gtk.Image(icon_name=icon, pixel_size=40, margin_top=14)
        self.append(image)
        name = Gtk.Label(label=label, wrap=True, justify=Gtk.Justification.CENTER)
        name.add_css_class("heading")
        self.append(name)
        n = Gtk.Label(label=words or (f"{count} app" if count == 1 else f"{count} apps"))
        n.add_css_class("dim-label")
        n.add_css_class("caption")
        self.append(n)
        self.append(Gtk.Box(vexpand=True))


class Window(Adw.ApplicationWindow):
    def __init__(self, **kwargs):
        super().__init__(**kwargs, title="KosherOS Store",
                         default_width=980, default_height=700)
        self.client = DaemonClient()
        self.installed: set[str] = set()
        # Everything installed on this machine as the daemon reports it,
        # which is not the same as "catalogue entries whose ref is
        # installed": an app approved once and installed is still here.
        self.installed_apps: list[dict] = []
        # ref -> (percent, what it is doing). Kept by the window, not by
        # the card, so rebuilding the grid never loses an install.
        self.working: dict[str, tuple[int, str]] = {}
        self.updates: dict[str, dict] = {}   # ref -> what is new about it
        self.cards: dict[str, AppCard] = {}
        self.can_install = True
        self.is_admin = False
        self.catalog: list[dict] = []
        # What kind of list this is ("approved" or "store") and whether
        # the daemon has the store's index yet; both change the words.
        self.access = "approved"
        self.ready = True
        self.shelf = "all"
        # How many cards the current shelf is showing (see PAGE_SIZE).
        self.page = PAGE_SIZE
        self._load_css()

        self.toasts = Adw.ToastOverlay()
        self.set_content(self.toasts)

        # The search box is always there, in the header, because looking for
        # something by name is what people do in a store.
        self.search = Gtk.SearchEntry(placeholder_text="Search apps",
                                      width_request=320)
        self.search.connect("search-changed", lambda _e: self._on_search())
        self.back = Gtk.Button(icon_name="go-previous-symbolic",
                               tooltip_text="All categories", visible=False)
        self.back.set_cursor_from_name("pointer")
        self.back.connect("clicked", lambda _b: self.go_home())
        header = Adw.HeaderBar(title_widget=self.search)
        header.pack_start(self.back)
        # One click for the lot, shown only on the Updates shelf.
        self.update_all = Gtk.Button(label="Update all", visible=False,
                                     tooltip_text="Update every app that has an update")
        self.update_all.add_css_class("suggested-action")
        self.update_all.set_cursor_from_name("pointer")
        self.update_all.connect("clicked", lambda _b: self._update_all())
        header.pack_end(self.update_all)
        # Asking the remote what is new. The shelf can only show an update
        # flatpak already knows about, and nothing else on the machine
        # fetches that, so the check is a thing a person can press.
        self.check_button = Gtk.Button(label="Check for updates", visible=False,
                                       tooltip_text="Ask Flathub whether the installed "
                                                    "apps have newer builds")
        self.check_button.set_cursor_from_name("pointer")
        self.check_button.connect("clicked", lambda _b: self._check_updates())
        header.pack_end(self.check_button)

        # Home: the categories themselves, which is what a store opens on.
        self.tiles = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE,
                                 homogeneous=True, column_spacing=14, row_spacing=14,
                                 min_children_per_line=2, max_children_per_line=4,
                                 valign=Gtk.Align.START, margin_top=6, margin_bottom=24)
        self.tiles.connect("child-activated", self._on_tile)
        home_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                           margin_start=18, margin_end=18, margin_top=18)
        self.heading = Gtk.Label(label="Approved apps", xalign=0)
        self.heading.add_css_class("title-2")
        home_box.append(self.heading)
        self.home_subtitle = Gtk.Label(xalign=0, wrap=True)
        self.home_subtitle.add_css_class("dim-label")
        home_box.append(self.home_subtitle)
        home_box.append(self.tiles)
        home_clamp = Adw.Clamp(maximum_size=980)
        home_clamp.set_child(home_box)
        home_scroller = Gtk.ScrolledWindow(vexpand=True,
                                           hscrollbar_policy=Gtk.PolicyType.NEVER)
        home_scroller.set_child(home_clamp)

        # Browsing or searching: the categories move to a sidebar and the
        # results fill the page beside them.
        self.sidebar = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE,
                                   margin_top=8, margin_start=8, margin_end=8)
        self.sidebar.add_css_class("navigation-sidebar")
        self.sidebar.connect("row-selected", self._on_sidebar)
        sidebar_scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER,
                                              width_request=210, vexpand=True)
        sidebar_scroller.set_child(self.sidebar)

        # Exactly two columns, or exactly one when the window is narrow.
        # Left to choose between one and two for itself, the grid and the
        # scrollbar chased each other — two columns made the cards narrow,
        # narrow cards made the list taller, a taller list wanted a
        # scrollbar, and the scrollbar left room for only one column. The
        # count now follows the window's width and nothing else.
        self.grid = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE,
                                homogeneous=True, column_spacing=12, row_spacing=12,
                                min_children_per_line=2, max_children_per_line=2,
                                margin_start=14, margin_end=14, margin_top=12,
                                margin_bottom=18, valign=Gtk.Align.START)
        # The rest of a long shelf, a page at a time, under the grid.
        self.more = Gtk.Button(halign=Gtk.Align.CENTER, margin_bottom=18, visible=False)
        self.more.add_css_class("pill")
        self.more.set_cursor_from_name("pointer")
        self.more.connect("clicked", lambda _b: self._show_more())
        grid_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        grid_box.append(self.grid)
        grid_box.append(self.more)
        grid_scroller = Gtk.ScrolledWindow(vexpand=True, hexpand=True,
                                           hscrollbar_policy=Gtk.PolicyType.NEVER)
        grid_scroller.set_child(grid_box)
        self.empty = Adw.StatusPage(icon_name="system-search-symbolic", vexpand=True)
        self.results = Gtk.Stack()
        self.results.add_named(grid_scroller, "grid")
        self.results.add_named(self.empty, "empty")

        browse = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        browse.append(sidebar_scroller)
        browse.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))
        browse.append(self.results)

        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.stack.add_named(home_scroller, "home")
        self.stack.add_named(browse, "browse")

        view = Adw.ToolbarView()
        view.add_top_bar(header)
        view.set_content(self.stack)
        self.toasts.set_child(view)

        narrow = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 880sp"))
        narrow.add_setter(self.grid, "min-children-per-line", 1)
        narrow.add_setter(self.grid, "max-children-per-line", 1)
        self.add_breakpoint(narrow)

        self.client.connect_app_signals(self._on_progress, self._on_finished)
        self.reload()

    @staticmethod
    def _load_css() -> None:
        display = Gdk.Display.get_default()
        if display is None:
            return
        provider = Gtk.CssProvider()
        provider.load_from_data(CSS + b"\n" + _tint_css())
        Gtk.StyleContext.add_provider_for_display(
            display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

    def toast(self, text: str) -> None:
        self.toasts.add_toast(Adw.Toast(title=text, timeout=5))

    # -- data ------------------------------------------------------------------

    def reload(self) -> None:
        import os

        def load():
            access, ready = "approved", True
            try:
                store = self.client.list_store_apps()
                catalog = store.get("apps", [])
                access = store.get("access", "approved")
                ready = bool(store.get("ready", True))
            except Exception:  # noqa: BLE001 - an older daemon has only the approved list
                catalog = self.client.list_catalog().get("apps", [])
            installed = set(self.client.list_installed())
            details = []
            try:
                details = self.client.list_installed_details()
            except Exception:  # noqa: BLE001 - an older daemon has only the refs
                pass
            policy = None
            try:
                policy = self.client.get_policy()
            except Exception:  # noqa: BLE001 - non-admins can read policy too,
                pass          # but never fail the store if they cannot
            updates = {}
            try:
                updates = {u["ref"]: u for u in self.client.list_app_updates()}
            except Exception:  # noqa: BLE001 - a store with no update news still opens
                pass
            return catalog, installed, details, policy, updates, access, ready

        def on_done(result):
            (self.catalog, self.installed, self.installed_apps, policy, self.updates,
             self.access, self.ready) = result
            if policy:
                me = next((u for u in policy["users"] if u["uid"] == os.getuid()), None)
                if me is not None:
                    self.can_install = me.get("can_install_apps", True)
                    self.is_admin = me.get("admin", False)
            self._render_home()
            self._render_sidebar()
            self._render()

        _run_async(load, on_done, lambda e: self.toast(_error_text(e)))

    def state_of(self, ref: str) -> str:
        if ref in self.working:
            return "working"
        if ref in self.updates:
            return "update"
        return "installed" if ref in self.installed else "available"

    def work_of(self, ref: str) -> tuple[int, str]:
        """How far along this app is, if anything is happening to it."""
        return self.working.get(ref, (0, "Queued…"))

    def work_started(self, ref: str, status: str = "Queued…") -> None:
        """Remember that an app is being worked on. The card used to hold
        this by itself and forgot the moment the grid was rebuilt, so an
        app being installed showed an Install button, and pressing it
        asked for the same install twice — which is an error, not a wish."""
        fresh = ref not in self.working
        self.working[ref] = (0, status)
        if fresh:
            self._refresh_counts()

    def work_ended(self, ref: str) -> None:
        self.working.pop(ref, None)
        self._refresh_counts()

    def _refresh_counts(self) -> None:
        """The shelves that come and go: Installing, Updates, Installed."""
        self._render_home()
        self._render_sidebar()
        if self.shelf == "installing":
            self._render()

    @staticmethod
    def _readable(ref: str) -> str:
        """'org.gnome.Boxes' -> 'Boxes'. What to call an app the catalogue
        does not describe, rather than showing a person a ref."""
        return (ref.rsplit(".", 1)[-1] or ref).strip() or ref

    def installed_entries(self) -> list[dict]:
        """Every app installed on this computer, best names first.

        Taken from the daemon's own account of what is installed rather
        than by filtering the catalogue to refs that happen to be
        installed: an app that was approved when it was installed and has
        since left the list is still on the machine, and the Installed
        shelf that filtered the catalogue showed nothing at all for it.
        """
        catalog = {a["ref"]: a for a in self.catalog}
        if not self.installed_apps:
            return [catalog[ref] for ref in sorted(self.installed) if ref in catalog]
        out = []
        for detail in self.installed_apps:
            ref = detail.get("ref")
            if not ref:
                continue
            entry = dict(catalog.get(ref) or {"ref": ref})
            entry["name"] = entry.get("name") or detail.get("name") or self._readable(ref)
            if detail.get("icon_name") and not entry.get("icon_name"):
                entry["icon_name"] = detail["icon_name"]
            if not entry.get("summary"):
                entry["summary"] = (
                    "Installed on this computer" if detail.get("approved", True)
                    else "Installed, but no longer on the approved list")
            out.append(entry)
        out.sort(key=lambda a: a["name"].lower())
        return out

    def installing_entries(self) -> list[dict]:
        """What is being installed, updated or removed right now — including
        work another window started, which arrives on the same signals."""
        catalog = {a["ref"]: a for a in self.catalog}
        known = {a["ref"]: a for a in self.installed_entries()}
        out = []
        for ref in sorted(self.working):
            entry = dict(catalog.get(ref) or known.get(ref) or {"ref": ref})
            entry["name"] = entry.get("name") or self._readable(ref)
            if not entry.get("summary"):
                entry["summary"] = self.working[ref][1] or "Working…"
            out.append(entry)
        return out

    def update_words(self, ref: str) -> str:
        """'Version 2.1 is available' / 'A newer build is available'."""
        info = self.updates.get(ref) or {}
        return (f"Version {info['version']} is available" if info.get("version")
                else "A newer build is available")

    def shelf_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {"all": len(self.catalog),
                                  "installed": len(self.installed_entries()),
                                  "installing": len(self.working),
                                  "updates": len([a for a in self.catalog
                                                  if a["ref"] in self.updates])}
        for app in self.catalog:
            key = shelf_of(app)
            counts[key] = counts.get(key, 0) + 1
        return counts

    def shelves_with_apps(self) -> list[tuple[str, str]]:
        counts = self.shelf_counts()
        found = [(key, label) for key, label, _claims in SHELVES if counts.get(key)]
        if counts.get("other"):
            found.append(("other", "Everything else"))
        return found

    # -- home ---------------------------------------------------------------------

    def go_home(self) -> None:
        self.search.set_text("")
        self.shelf = "all"
        self.stack.set_visible_child_name("home")
        self.back.set_visible(False)

    def _render_home(self) -> None:
        while (child := self.tiles.get_first_child()) is not None:
            self.tiles.remove(child)
        counts = self.shelf_counts()
        self.heading.set_label("Apps" if self.access == "store" else "Approved apps")
        self.home_subtitle.set_label(self.home_words(counts["all"]))
        tiles = []
        if counts["installing"]:
            tiles.append(("installing", "Installing", "folder-download-symbolic"))
        # Updates is always here, with or without one waiting: a shelf that
        # appeared only when something needed updating was a feature nobody
        # could find — "where is the feature to update apps".
        tiles.append(("updates", "Updates", "software-update-available-symbolic"))
        if counts["installed"]:
            tiles.append(("installed", "Installed", "emblem-ok-symbolic"))
        tiles += [(key, label, SHELF_ICONS.get(key, (FALLBACK_ICON,))[0])
                  for key, label in self.shelves_with_apps()]
        tiles.append(("all", "All apps", "view-grid-symbolic"))
        for key, label, icon in tiles:
            child = Gtk.FlowBoxChild()
            words = "Up to date" if key == "updates" and not counts["updates"] else ""
            child.set_child(ShelfTile(key, label, counts.get(key, 0), icon, words))
            child.key = key
            child.set_cursor_from_name("pointer")
            self.tiles.append(child)

    def home_words(self, total: int) -> str:
        """What the home page says under its heading: how many apps, and
        where they come from — an administrator's list, or the store."""
        if self.access == "store":
            words = (f"{total} apps from the store, minus what is not for this "
                     "account. Pick a category, or search."
                     if total else "The store is on its way.")
            if not self.ready:
                words += (" The rest of the store is still being downloaded; "
                          "the approved apps are here already.")
            return words
        if total:
            return (f"{total} apps an administrator has approved for this computer. "
                    "Pick a category, or search.")
        return ("An administrator chooses which apps this computer may install, "
                "in KosherOS Admin.")

    def _on_tile(self, _box, child) -> None:
        self.show_shelf(getattr(child, "key", "all"))

    # -- sidebar -------------------------------------------------------------------

    def _render_sidebar(self) -> None:
        while (child := self.sidebar.get_first_child()) is not None:
            self.sidebar.remove(child)
        counts = self.shelf_counts()
        rows = [("all", "All apps"), ("updates", "Updates"), ("installed", "Installed")]
        if counts["installing"]:
            rows.insert(0, ("installing", "Installing"))
        rows += self.shelves_with_apps()
        for key, label in rows:
            row = Gtk.ListBoxRow()
            row.key = key
            box = Gtk.Box(spacing=10, margin_top=6, margin_bottom=6,
                          margin_start=6, margin_end=6)
            icon = ("view-grid-symbolic" if key == "all" else
                    "emblem-ok-symbolic" if key == "installed" else
                    "folder-download-symbolic" if key == "installing" else
                    "software-update-available-symbolic" if key == "updates" else
                    SHELF_ICONS.get(key, (FALLBACK_ICON,))[0])
            box.append(Gtk.Image(icon_name=icon, pixel_size=16))
            box.append(Gtk.Label(label=label, xalign=0, hexpand=True, ellipsize=3))
            count = Gtk.Label(label=str(counts.get(key, 0)))
            count.add_css_class("dim-label")
            count.add_css_class("caption")
            box.append(count)
            row.set_child(box)
            row.set_cursor_from_name("pointer")
            self.sidebar.append(row)
        self._select_sidebar(self.shelf)

    def _select_sidebar(self, key: str) -> None:
        row = self.sidebar.get_first_child()
        while row is not None:
            if getattr(row, "key", None) == key:
                self.sidebar.select_row(row)
                return
            row = row.get_next_sibling()

    def _on_sidebar(self, _list, row) -> None:
        if row is None:
            return
        key = getattr(row, "key", "all")
        if key != self.shelf:
            self.shelf = key
            self.page = PAGE_SIZE
            self._render()

    # -- searching and browsing ------------------------------------------------------

    def show_shelf(self, key: str) -> None:
        self.shelf = key
        self.page = PAGE_SIZE
        self.stack.set_visible_child_name("browse")
        self.back.set_visible(True)
        self._select_sidebar(key)
        self._render()

    def _on_search(self) -> None:
        text = self.search.get_text().strip()
        self.page = PAGE_SIZE
        if text and self.stack.get_visible_child_name() != "browse":
            # Searching takes you into the shelves, across everything.
            self.shelf = "all"
            self.stack.set_visible_child_name("browse")
            self.back.set_visible(True)
            self._select_sidebar("all")
        self._render()

    def shown_apps(self) -> list[dict]:
        needle = self.search.get_text().strip().lower()
        if self.shelf == "installed":
            pool = self.installed_entries()
        elif self.shelf == "installing":
            pool = self.installing_entries()
        else:
            pool = self.catalog
        found = []
        for app in pool:
            if self.shelf == "updates":
                if app["ref"] not in self.updates:
                    continue
            elif self.shelf not in ("all", "installed", "installing") \
                    and shelf_of(app) != self.shelf:
                continue
            if needle and needle not in app.get("name", "").lower() \
                    and needle not in app["ref"].lower() \
                    and needle not in (app.get("summary") or "").lower():
                continue
            found.append(app)
        found.sort(key=lambda a: a.get("name", a["ref"]).lower())
        return found

    def _render(self) -> None:
        while (child := self.grid.get_first_child()) is not None:
            self.grid.remove(child)
        self.cards.clear()
        shown = self.shown_apps()
        browsing = self.stack.get_visible_child_name() == "browse"
        self.update_all.set_visible(self.shelf == "updates" and bool(shown) and browsing)
        self.check_button.set_visible(self.shelf == "updates" and browsing)
        if not shown:
            self.results.set_visible_child_name("empty")
            self.more.set_visible(False)
            if not self.catalog and self.access == "store":
                self.empty.set_title("The store is on its way")
                self.empty.set_description(
                    "This computer is still downloading the list of apps. "
                    "Try again in a few minutes.")
            elif not self.catalog:
                self.empty.set_title("No apps are approved yet")
                self.empty.set_description(
                    "An administrator chooses which apps this computer may install, "
                    "in KosherOS Admin.")
            elif self.search.get_text().strip():
                self.empty.set_title("Nothing matches that")
                self.empty.set_description("Try a different word, or another category.")
            elif self.shelf == "updates":
                self.empty.set_title("Everything is up to date")
                self.empty.set_description("Installed apps are updated from here when a "
                                           "newer build is available.")
            elif self.shelf == "installed":
                self.empty.set_title("Nothing is installed yet")
                self.empty.set_description("Apps you install from here appear in this "
                                           "list, and can be removed from it.")
            elif self.shelf == "installing":
                self.empty.set_title("Nothing is being installed")
                self.empty.set_description("An app appears here while it is being "
                                           "installed, updated or removed.")
            else:
                self.empty.set_title("Nothing in this category")
                self.empty.set_description("Try another category.")
            return
        self.results.set_visible_child_name("grid")
        for app in shown[:self.page]:
            card = AppCard(app, self)
            self.cards[card.ref] = card
            child = Gtk.FlowBoxChild()
            child.set_child(card)
            self.grid.append(child)
        left = len(shown) - self.page
        self.more.set_visible(left > 0)
        if left > 0:
            self.more.set_label(f"Show {min(left, PAGE_SIZE)} more of {len(shown)}")

    def _show_more(self) -> None:
        self.page += PAGE_SIZE
        self._render()

    def _check_updates(self) -> None:
        """Ask the daemon to fetch the remote's news and say what is new."""
        self.check_button.set_sensitive(False)
        self.check_button.set_label("Checking…")

        def done(found):
            self.updates = {u["ref"]: u for u in found}
            self.check_button.set_sensitive(True)
            self.check_button.set_label("Check for updates")
            self._render_home()
            self._render_sidebar()
            self._render()
            self.toast(f"{len(found)} app can be updated" if len(found) == 1
                       else f"{len(found)} apps can be updated" if found
                       else "Every app is up to date")

        def failed(e):
            self.check_button.set_sensitive(True)
            self.check_button.set_label("Check for updates")
            self.toast(_error_text(e))

        _run_async(self.client.check_app_updates, done, failed)

    def _update_all(self) -> None:
        for ref in list(self.updates):
            self.work_started(ref)
        for ref, card in self.cards.items():
            if ref in self.updates:
                card.set_state("working", 0, "Queued…")

        def on_done(count):
            self.toast(f"Updating {count} app" + ("" if count == 1 else "s"))

        def on_error(e):
            self._render()
            self.toast(_error_text(e))

        _run_async(self.client.update_all_apps, on_done, on_error)

    # -- progress ------------------------------------------------------------------

    def _on_progress(self, ref: str, percent: int, status: str) -> None:
        fresh = ref not in self.working
        self.working[ref] = (percent, status)
        card = self.cards.get(ref)
        if card is not None:
            card.set_state("working", percent, status)
        if fresh:
            # Something this window did not start — the admin app, or an
            # update — is now in hand. It belongs on the Installing shelf.
            self._refresh_counts()

    def _on_finished(self, ref: str, ok: bool, error: str) -> None:
        self.working.pop(ref, None)
        card = self.cards.get(ref)
        name = card.app.get("name", ref) if card else ref
        if ok:
            if ref in self.updates:
                self.toast(f"{name} updated")
            else:
                self.toast(f"{name} is ready" if ref not in self.installed
                           else f"{name} removed")
            self.reload()
        else:
            if card is not None:
                card.set_state(self.state_of(ref))
            self._refresh_counts()
            self.toast(error or f"{name} failed")


class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)

    def do_activate(self):
        (self.get_active_window() or Window(application=self)).present()


def main() -> int:
    return App().run(None)


if __name__ == "__main__":
    raise SystemExit(main())
