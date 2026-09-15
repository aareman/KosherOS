"""KosherOS Store — browse and install approved applications.

Open to every user: the catalog is the allowlist, so anything listed here
is already approved by the admin. Installs are performed by kosherd (this
app has no privileges of its own) and progress arrives over D-Bus.

It looks like a store — shelves across the top, a grid of apps with their
icons — and it opens instantly, because everything it draws is local: the
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
.shelf-bar { padding: 6px 12px; }
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


class Window(Adw.ApplicationWindow):
    def __init__(self, **kwargs):
        super().__init__(**kwargs, title="KosherOS Store",
                         default_width=900, default_height=660)
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

        header = Adw.HeaderBar()
        self.search_button = Gtk.ToggleButton(icon_name="system-search-symbolic",
                                              tooltip_text="Search")
        self.search_button.set_cursor_from_name("pointer")
        header.pack_end(self.search_button)

        self.search = Gtk.SearchEntry(placeholder_text="Search approved apps",
                                      hexpand=True)
        self.search.connect("search-changed", lambda _e: self._render())
        search_bar = Gtk.SearchBar(child=self.search, show_close_button=False)
        search_bar.set_key_capture_widget(self)
        search_bar.connect_entry(self.search)
        self.search_button.bind_property(
            "active", search_bar, "search-mode-enabled",
            2 | 1)  # BIDIRECTIONAL | SYNC_CREATE

        # The shelves, as one row of pills that scrolls if it must.
        self.shelf_box = Gtk.Box(spacing=6)
        self.shelf_box.add_css_class("shelf-bar")
        shelf_scroller = Gtk.ScrolledWindow(
            vscrollbar_policy=Gtk.PolicyType.NEVER,
            hscrollbar_policy=Gtk.PolicyType.AUTOMATIC, propagate_natural_height=True)
        shelf_scroller.set_child(self.shelf_box)

        self.grid = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE,
                                homogeneous=True, column_spacing=12, row_spacing=12,
                                min_children_per_line=1, max_children_per_line=3,
                                margin_start=12, margin_end=12, margin_top=6,
                                margin_bottom=18, valign=Gtk.Align.START)
        clamp = Adw.Clamp(maximum_size=1100, tightening_threshold=900)
        clamp.set_child(self.grid)
        scroller = Gtk.ScrolledWindow(vexpand=True,
                                      hscrollbar_policy=Gtk.PolicyType.NEVER)
        scroller.set_child(clamp)

        self.empty = Adw.StatusPage(icon_name="view-grid-symbolic", vexpand=True)
        self.stack = Gtk.Stack()
        self.stack.add_named(scroller, "grid")
        self.stack.add_named(self.empty, "empty")

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        body.append(search_bar)
        body.append(shelf_scroller)
        body.append(Gtk.Separator())
        body.append(self.stack)

        view = Adw.ToolbarView()
        view.add_top_bar(header)
        view.set_content(body)
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
            self._render_shelves()
            self._render()

        _run_async(load, on_done, lambda e: self.toast(_error_text(e)))

    # -- shelves ---------------------------------------------------------------

    def shelf_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {"all": len(self.catalog),
                                  "installed": len([a for a in self.catalog
                                                    if a["ref"] in self.installed])}
        for app in self.catalog:
            key = shelf_of(app)
            counts[key] = counts.get(key, 0) + 1
        return counts

    def _render_shelves(self) -> None:
        while (child := self.shelf_box.get_first_child()) is not None:
            self.shelf_box.remove(child)
        counts = self.shelf_counts()
        first = None
        # Only shelves with something on them, so a small catalogue does not
        # show eight empty tabs.
        wanted = [("all", "All"), ("installed", "Installed")]
        wanted += [(key, label) for key, label, _c in SHELVES if counts.get(key)]
        if counts.get("other"):
            wanted.append(("other", "Everything else"))
        for key, label in wanted:
            n = counts.get(key, 0)
            button = Gtk.ToggleButton(label=f"{label}  {n}" if n else label)
            button.add_css_class("pill")
            button.set_cursor_from_name("pointer")
            button.shelf = key
            if first is None:
                first = button
            else:
                button.set_group(first)
            button.set_active(key == self.shelf)
            button.connect("toggled", self._on_shelf)
            self.shelf_box.append(button)

    def _on_shelf(self, button) -> None:
        if button.get_active() and button.shelf != self.shelf:
            self.shelf = button.shelf
            self._render()

    # -- the grid ----------------------------------------------------------------

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
            self.stack.set_visible_child_name("empty")
            if not self.catalog:
                self.empty.set_title("No apps are approved yet")
                self.empty.set_description(
                    "An administrator chooses which apps this computer may install, "
                    "in KosherOS Admin.")
            elif self.search.get_text().strip():
                self.empty.set_title("Nothing matches that")
                self.empty.set_description("Try a different word, or another shelf.")
            else:
                self.empty.set_title("Nothing on this shelf")
                self.empty.set_description("Try another shelf.")
            return
        self.stack.set_visible_child_name("grid")
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
