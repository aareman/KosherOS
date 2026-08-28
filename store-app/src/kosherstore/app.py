"""KosherOS Store — browse and install approved applications.

Open to every user: the catalog is the allowlist, so anything listed here
is already approved by the admin. Installs are performed by kosherd (this
app has no privileges of its own) and progress arrives over D-Bus.
"""

from __future__ import annotations

import threading

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402

from kosherd.client import DaemonClient  # noqa: E402

APP_ID = "org.kosherlinux.Store"


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


class AppRow(Adw.ActionRow):
    def __init__(self, app: dict, store: "Window"):
        super().__init__(title=app.get("name", app["ref"]),
                         subtitle=app.get("summary", app["ref"]))
        self.ref = app["ref"]
        self.store = store

        self.button = Gtk.Button(valign=Gtk.Align.CENTER)
        self.button.connect("clicked", self._on_clicked)
        self.progress = Gtk.ProgressBar(valign=Gtk.Align.CENTER, hexpand=True,
                                        show_text=True, visible=False)
        self.add_suffix(self.progress)
        self.add_suffix(self.button)
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
            self.button.set_css_classes(["destructive-action"])
            self.button.set_sensitive(self.store.is_admin)
            self.button.set_tooltip_text(
                None if self.store.is_admin else "Only an admin can remove apps")
        else:
            self.button.set_label("Install")
            self.button.set_css_classes(["suggested-action"])
            self.button.set_sensitive(self.store.can_install)
            self.button.set_tooltip_text(
                None if self.store.can_install else
                "App installation is turned off for your account")

    def _on_clicked(self, _b) -> None:
        installing = self.state != "installed"
        self.set_state("working", 0, "Starting…")
        work = (self.store.client.install_app if installing
                else self.store.client.remove_app)

        def on_error(e):
            self.set_state("installed" if not installing else "available")
            self.store.toast(_error_text(e))

        _run_async(lambda: work(self.ref), lambda _r: None, on_error)


class Window(Adw.ApplicationWindow):
    def __init__(self, **kwargs):
        super().__init__(**kwargs, title="KosherOS Store",
                         default_width=680, default_height=620)
        self.client = DaemonClient()
        self.installed: set[str] = set()
        self.rows: dict[str, AppRow] = {}
        self.can_install = True
        self.is_admin = False

        self.toasts = Adw.ToastOverlay()
        self.set_content(self.toasts)

        self.search = Gtk.SearchEntry(placeholder_text="Search approved apps",
                                      margin_start=12, margin_end=12,
                                      margin_top=12, margin_bottom=6)
        self.search.connect("search-changed", lambda _e: self._rebuild())

        self.list_box = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE,
                                    margin_start=12, margin_end=12, margin_bottom=12)
        self.list_box.add_css_class("boxed-list")
        scroller = Gtk.ScrolledWindow(vexpand=True)
        scroller.set_child(self.list_box)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.append(Adw.HeaderBar())
        box.append(self.search)
        box.append(scroller)
        self.toasts.set_child(box)

        self.client.connect_app_signals(self._on_progress, self._on_finished)
        self.catalog: list[dict] = []
        self.reload()

    def toast(self, text: str) -> None:
        self.toasts.add_toast(Adw.Toast(title=text, timeout=5))

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
            self._rebuild()

        _run_async(load, on_done, lambda e: self.toast(_error_text(e)))

    def _rebuild(self) -> None:
        while (child := self.list_box.get_first_child()) is not None:
            self.list_box.remove(child)
        self.rows.clear()
        needle = self.search.get_text().strip().lower()
        shown = [a for a in self.catalog
                 if needle in a.get("name", "").lower() or needle in a["ref"].lower()]
        if not shown:
            empty = Adw.ActionRow(title="No approved apps match" if self.catalog
                                  else "No apps are approved yet")
            empty.set_sensitive(False)
            self.list_box.append(empty)
            return
        for app in sorted(shown, key=lambda a: a.get("name", a["ref"]).lower()):
            row = AppRow(app, self)
            self.rows[row.ref] = row
            self.list_box.append(row)

    def _on_progress(self, ref: str, percent: int, status: str) -> None:
        row = self.rows.get(ref)
        if row is not None:
            row.set_state("working", percent, status)

    def _on_finished(self, ref: str, ok: bool, error: str) -> None:
        row = self.rows.get(ref)
        if ok:
            self.toast(f"{row.get_title() if row else ref} is ready"
                       if ref not in self.installed else f"{ref} removed")
            self.reload()
        else:
            if row is not None:
                row.set_state("installed" if ref in self.installed else "available")
            self.toast(error or f"{ref} failed")


class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)

    def do_activate(self):
        (self.get_active_window() or Window(application=self)).present()


def main() -> int:
    return App().run(None)


if __name__ == "__main__":
    raise SystemExit(main())
