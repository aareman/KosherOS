"""KosherOS Store — browse and install approved applications.

Open to every user: the catalog is the allowlist, so anything listed here
is already approved by the admin. Installs are performed by kosherd (this
app has no privileges of its own) and progress arrives over D-Bus.

It looks like a store — it opens on the categories, a search box is always
in the header, and choosing a category (or searching) moves the categories
into a sidebar with the apps beside them — and it opens instantly, because everything it draws is local: the
approved list is one small file kosherd writes, and each icon is a file
flatpak already downloaded beside the remote's catalogue. Nothing here
parses the 40 MB app index or touches the network.
"""

from __future__ import annotations

import threading

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from kosherd.client import DaemonClient  # noqa: E402

APP_ID = "org.kosherlinux.Store"

# The shelves, in the order a person browses them, and the freedesktop
# categories each gathers. An app lands on the first shelf that claims one
# of its categories, so nothing appears twice; anything unclaimed falls
# under Everything else.
SHELVES = (
    ("internet", "Internet", ("Network", "WebBrowser", "Email", "Chat", "News")),
    ("work", "Work", ("Office", "Finance", "Spreadsheet", "WordProcessor",
                      "Presentation", "Calendar", "ProjectManagement")),
    ("learning", "Learning", ("Education", "Science", "Languages", "Math",
                              "Astronomy", "Geography")),
    ("pictures", "Pictures & video", ("Graphics", "Photography", "AudioVideo",
                                      "Video", "2DGraphics", "3DGraphics",
                                      "RasterGraphics", "VectorGraphics", "Viewer")),
    ("music", "Music", ("Audio", "Music", "Player", "Recorder")),
    ("games", "Games", ("Game", "ArcadeGame", "BoardGame", "LogicGame",
                        "KidsGame", "Puzzle", "Simulation", "Sports")),
    ("develop", "Developer tools", ("Development", "IDE", "TextEditor",
                                    "Debugger", "WebDevelopment")),
    ("utilities", "Utilities", ("Utility", "System", "Settings", "Accessibility",
                                "Archiving", "FileTools", "TerminalEmulator",
                                "Security")),
)
ICON_SIZE = 48
FALLBACK_ICON = "application-x-executable"
# When an app has no icon of its own on this machine, draw the one that says
# what KIND of app it is rather than the same grey box twenty times. Tried in
# order against the icon theme.
SHELF_ICONS = {
    "internet": ("web-browser", "applications-internet"),
    "work": ("x-office-document", "applications-office"),
    "learning": ("applications-science", "accessories-dictionary"),
    "pictures": ("applications-graphics", "image-x-generic"),
    "music": ("multimedia-player", "audio-x-generic"),
    "games": ("applications-games", "input-gaming"),
    "develop": ("applications-engineering", "text-x-script"),
    "utilities": ("applications-utilities", "applications-system"),
    "other": (FALLBACK_ICON,),
}

CSS = b"""
.app-card { padding: 12px 14px; }
.app-card button.pill { min-height: 26px; padding: 2px 16px; font-size: 0.92em; }
.app-card:hover { background-color: alpha(currentColor, 0.04); }
.shelf-tile { padding: 8px; }
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
    categories = set(app.get("categories") or ())
    for key, _label, claims in SHELVES:
        if categories & set(claims):
            return key
    return "other"


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


def app_icon(app: dict, size: int = ICON_SIZE) -> Gtk.Widget:
    """The app's icon: the file flatpak cached, else a themed icon, else the
    one for its shelf. Never a network fetch, never an error."""
    from kosherd import apps as apps_mod

    image = Gtk.Image(pixel_size=size)
    try:
        path = apps_mod.icon_file(app, size)
    except Exception:  # noqa: BLE001 - an icon is never worth a failure
        path = ""
    if path:
        image.set_from_file(path)
        return image
    display = Gdk.Display.get_default()
    theme = Gtk.IconTheme.get_for_display(display) if display is not None else None
    for name in icon_candidates(app):
        if theme is None or theme.has_icon(name):
            image.set_from_icon_name(name)
            return image
    image.set_from_icon_name(FALLBACK_ICON)
    return image


class AppCard(Gtk.Box):
    """One app: icon, name, what it is, and the button that acts on it."""

    def __init__(self, app: dict, store: "Window"):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6,
                         width_request=280)
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
        self.set_state("installed" if self.ref in store.installed else "available")

    def set_state(self, state: str, percent: int = 0, status: str = "") -> None:
        self.state = state
        busy = state == "working"
        self.progress.set_visible(busy)
        self.button.set_visible(not busy)
        if busy:
            self.progress.set_fraction(percent / 100)
            self.progress.set_text(status or f"{percent}%")
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
        installing = self.state != "installed"
        # Several apps can be requested at once; kosherd queues them.
        self.set_state("working", 0, "Queued…")
        work = (self.store.client.install_app if installing
                else self.store.client.remove_app)

        def on_error(e):
            self.set_state("installed" if not installing else "available")
            self.store.toast(_error_text(e))

        _run_async(lambda: work(self.ref), lambda _r: None, on_error)


class ShelfTile(Gtk.Box):
    """One category on the home page: what it is, and how much is on it."""

    def __init__(self, key: str, label: str, count: int, icon: str):
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
        n = Gtk.Label(label=f"{count} app" if count == 1 else f"{count} apps")
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
        self.cards: dict[str, AppCard] = {}
        self.can_install = True
        self.is_admin = False
        self.catalog: list[dict] = []
        self.shelf = "all"
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

        # Home: the categories themselves, which is what a store opens on.
        self.tiles = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE,
                                 homogeneous=True, column_spacing=14, row_spacing=14,
                                 min_children_per_line=2, max_children_per_line=4,
                                 valign=Gtk.Align.START, margin_top=6, margin_bottom=24)
        self.tiles.connect("child-activated", self._on_tile)
        home_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                           margin_start=18, margin_end=18, margin_top=18)
        heading = Gtk.Label(label="Approved apps", xalign=0)
        heading.add_css_class("title-2")
        home_box.append(heading)
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

        self.grid = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE,
                                homogeneous=True, column_spacing=12, row_spacing=12,
                                min_children_per_line=1, max_children_per_line=2,
                                margin_start=14, margin_end=14, margin_top=12,
                                margin_bottom=18, valign=Gtk.Align.START)
        grid_scroller = Gtk.ScrolledWindow(vexpand=True, hexpand=True,
                                           hscrollbar_policy=Gtk.PolicyType.NEVER)
        grid_scroller.set_child(self.grid)
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

        self.client.connect_app_signals(self._on_progress, self._on_finished)
        self.reload()

    @staticmethod
    def _load_css() -> None:
        display = Gdk.Display.get_default()
        if display is None:
            return
        provider = Gtk.CssProvider()
        provider.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_display(
            display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

    def toast(self, text: str) -> None:
        self.toasts.add_toast(Adw.Toast(title=text, timeout=5))

    # -- data ------------------------------------------------------------------

    def reload(self) -> None:
        import os

        def load():
            catalog = self.client.list_catalog().get("apps", [])
            installed = set(self.client.list_installed())
            policy = None
            try:
                policy = self.client.get_policy()
            except Exception:  # noqa: BLE001 - non-admins can read policy too,
                pass          # but never fail the store if they cannot
            return catalog, installed, policy

        def on_done(result):
            self.catalog, self.installed, policy = result
            if policy:
                me = next((u for u in policy["users"] if u["uid"] == os.getuid()), None)
                if me is not None:
                    self.can_install = me.get("can_install_apps", True)
                    self.is_admin = me.get("admin", False)
            self._render_home()
            self._render_sidebar()
            self._render()

        _run_async(load, on_done, lambda e: self.toast(_error_text(e)))

    def shelf_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {"all": len(self.catalog),
                                  "installed": len([a for a in self.catalog
                                                    if a["ref"] in self.installed])}
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
        self.home_subtitle.set_label(
            f"{counts['all']} apps an administrator has approved for this computer. "
            "Pick a category, or search."
            if counts["all"] else
            "An administrator chooses which apps this computer may install, "
            "in KosherOS Admin.")
        tiles = [("installed", "Installed", "emblem-ok-symbolic")] if counts["installed"] else []
        tiles += [(key, label, SHELF_ICONS.get(key, (FALLBACK_ICON,))[0])
                  for key, label in self.shelves_with_apps()]
        tiles.append(("all", "All apps", "view-grid-symbolic"))
        for key, label, icon in tiles:
            child = Gtk.FlowBoxChild()
            child.set_child(ShelfTile(key, label, counts.get(key, 0), icon))
            child.key = key
            child.set_cursor_from_name("pointer")
            self.tiles.append(child)

    def _on_tile(self, _box, child) -> None:
        self.show_shelf(getattr(child, "key", "all"))

    # -- sidebar -------------------------------------------------------------------

    def _render_sidebar(self) -> None:
        while (child := self.sidebar.get_first_child()) is not None:
            self.sidebar.remove(child)
        counts = self.shelf_counts()
        rows = [("all", "All apps"), ("installed", "Installed")]
        rows += self.shelves_with_apps()
        for key, label in rows:
            row = Gtk.ListBoxRow()
            row.key = key
            box = Gtk.Box(spacing=10, margin_top=6, margin_bottom=6,
                          margin_start=6, margin_end=6)
            icon = ("view-grid-symbolic" if key == "all" else
                    "emblem-ok-symbolic" if key == "installed" else
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
            self._render()

    # -- searching and browsing ------------------------------------------------------

    def show_shelf(self, key: str) -> None:
        self.shelf = key
        self.stack.set_visible_child_name("browse")
        self.back.set_visible(True)
        self._select_sidebar(key)
        self._render()

    def _on_search(self) -> None:
        text = self.search.get_text().strip()
        if text and self.stack.get_visible_child_name() != "browse":
            # Searching takes you into the shelves, across everything.
            self.shelf = "all"
            self.stack.set_visible_child_name("browse")
            self.back.set_visible(True)
            self._select_sidebar("all")
        self._render()

    def shown_apps(self) -> list[dict]:
        needle = self.search.get_text().strip().lower()
        found = []
        for app in self.catalog:
            if self.shelf == "installed":
                if app["ref"] not in self.installed:
                    continue
            elif self.shelf != "all" and shelf_of(app) != self.shelf:
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
        if not shown:
            self.results.set_visible_child_name("empty")
            if not self.catalog:
                self.empty.set_title("No apps are approved yet")
                self.empty.set_description(
                    "An administrator chooses which apps this computer may install, "
                    "in KosherOS Admin.")
            elif self.search.get_text().strip():
                self.empty.set_title("Nothing matches that")
                self.empty.set_description("Try a different word, or another category.")
            else:
                self.empty.set_title("Nothing in this category")
                self.empty.set_description("Try another category.")
            return
        self.results.set_visible_child_name("grid")
        for app in shown:
            card = AppCard(app, self)
            self.cards[card.ref] = card
            child = Gtk.FlowBoxChild()
            child.set_child(card)
            self.grid.append(child)

    # -- progress ------------------------------------------------------------------

    def _on_progress(self, ref: str, percent: int, status: str) -> None:
        card = self.cards.get(ref)
        if card is not None:
            card.set_state("working", percent, status)

    def _on_finished(self, ref: str, ok: bool, error: str) -> None:
        card = self.cards.get(ref)
        name = card.app.get("name", ref) if card else ref
        if ok:
            self.toast(f"{name} is ready" if ref not in self.installed
                       else f"{name} removed")
            self.reload()
        else:
            if card is not None:
                card.set_state("installed" if ref in self.installed else "available")
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
