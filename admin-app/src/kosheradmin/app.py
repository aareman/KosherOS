"""KosherOS Admin — GTK4/libadwaita client of kosherd.

The window: one password to unlock, then two tabs — the family board (home)
and the activity feed — with each person opening onto a full page of their
own. Every mutating call runs in a worker thread (the desktop polkit agent
may prompt, which blocks the call); UI updates hop back via GLib.idle_add.

The label tables the daemon's tests read live in labels.py; the pages in
family.py, feed.py and detail.py; the dialogs in dialogs.py.
"""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402

from kosherd.client import DaemonClient  # noqa: E402

from . import labels  # noqa: E402
from .common import error_text, load_css, run_async, submit_on_enter  # noqa: E402
from .detail import UserDetailPage  # noqa: E402
from .dialogs import add_person_dialog  # noqa: E402
from .family import FamilyPage  # noqa: E402
from .feed import ActivityPage  # noqa: E402

APP_ID = "org.kosherlinux.Admin"

# Re-exported so tests and older imports keep one place to look.
EDITABLE_LISTS = labels.EDITABLE_LISTS


class Window(Adw.ApplicationWindow):
    def __init__(self, **kwargs):
        super().__init__(**kwargs, title="KosherOS Admin", default_width=900,
                         default_height=680)
        load_css()
        self.client = DaemonClient()
        self.policy: dict = {"users": [], "guardian": {"enabled": False}}
        self.requests: list[dict] = []
        self.status: dict | None = None
        self.summary: dict = {}
        self.catalog_count: int | None = None
        self.update_state: str | None = None
        self.guardian_ok = False

        self.toasts = Adw.ToastOverlay()
        self.set_content(self.toasts)

        self.stack = Adw.ViewStack()
        self.family_page = FamilyPage(self)
        self.activity_page = ActivityPage(self)
        # Pages must not talk to the daemon before Unlock, or their calls
        # race the unlock prompt.
        self.stack.add_titled_with_icon(self.family_page, "family", "Family",
                                        "system-users-symbolic")
        self.stack.add_titled_with_icon(self.activity_page, "activity", "Activity",
                                        "document-open-recent-symbolic")
        self.stack.connect("notify::visible-child-name", self._on_tab)

        switcher = Adw.ViewSwitcher(stack=self.stack, policy=Adw.ViewSwitcherPolicy.WIDE)
        self.header = Adw.HeaderBar(title_widget=switcher)
        add_menu = Gio.Menu()
        add_menu.append("Create User Account…", "win.create-user")
        add_menu.append("Adopt Existing User…", "win.adopt-user")
        add_btn = Gtk.MenuButton(label="Add a person", menu_model=add_menu)
        self.header.pack_start(add_btn)
        lock_btn = Gtk.Button(icon_name="changes-prevent-symbolic", tooltip_text="Lock now")
        lock_btn.connect("clicked", lambda _b: self._lock())
        self.header.pack_end(lock_btn)
        for name, adopt in (("create-user", False), ("adopt-user", True)):
            action = Gio.SimpleAction(name=name)
            action.connect("activate", lambda _a, _p, a=adopt: add_person_dialog(self, a))
            self.add_action(action)

        view = Adw.ToolbarView()
        view.add_top_bar(self.header)
        view.set_content(self.stack)
        # A navigation view, so configuring one person gets a full page of
        # its own instead of an expander squeezed into the list.
        self.nav = Adw.NavigationView()
        self.nav.add(Adw.NavigationPage(child=view, title="KosherOS Admin", tag="root"))
        self.content = self.nav

        # Locked state: one password, then everything works.
        self.lock_view = Adw.StatusPage(
            icon_name="changes-prevent-symbolic",
            title="KosherOS Admin",
            description="Unlock once to see the family, answer requests and change settings.")
        unlock_btn = Gtk.Button(label="Unlock", halign=Gtk.Align.CENTER)
        unlock_btn.add_css_class("suggested-action")
        unlock_btn.add_css_class("pill")
        unlock_btn.connect("clicked", lambda _b: self._unlock())
        self.lock_view.set_child(unlock_btn)

        self.toasts.set_child(self.lock_view)
        self._unlock()  # prompt immediately on open

    def _not_an_admin(self) -> bool:
        """True when this account is not in kosher-admin.

        Without it, Unlock falls back to polkit's auth_admin, and KosherOS
        deliberately has no polkit admin identities — so the desktop shows a
        password prompt that cannot succeed no matter what is typed. Saying
        so plainly beats an unanswerable dialog.
        """
        import grp
        import os

        try:
            return os.getuid() not in (0,) and \
                os.getlogin() not in grp.getgrnam("kosher-admin").gr_mem
        except (KeyError, OSError):
            return False  # can't tell; let the normal path try

    def _unlock(self) -> None:
        if self._not_an_admin():
            self.lock_view.set_description(
                "This account is not an administrator of this computer, so it "
                "cannot change settings here. Ask whoever set the computer up.")
            self.toasts.set_child(self.lock_view)
            return

        def on_done(_r):
            self.toasts.set_child(self.content)
            self.reload()

        run_async(self.client.unlock, on_done, lambda e: (
            self.toasts.set_child(self.lock_view), self.toast(error_text(e))))

    def _lock(self) -> None:
        self.guardian_ok = False
        self.toasts.set_child(self.lock_view)
        run_async(self.client.lock, lambda _r: None, lambda e: self.toast(error_text(e)))

    # -- navigation ------------------------------------------------------------------

    def push(self, page: Adw.NavigationPage) -> None:
        self.nav.push(page)

    def open_user_detail(self, user: dict) -> None:
        self.nav.push(UserDetailPage(self, user))

    def show_activity(self, uid: int) -> None:
        """Jump to the activity tab, narrowed to one person."""
        self.nav.pop_to_tag("root")
        self.stack.set_visible_child_name("activity")
        self.activity_page.show_user(uid)

    def _on_tab(self, stack, _param) -> None:
        if stack.get_visible_child_name() == "activity":
            self.activity_page.refresh()

    def refresh_open_detail(self) -> None:
        """After a policy reload, rebuild the detail page being viewed so it
        shows the new state instead of the dict it was opened with."""
        page = self.nav.get_visible_page()
        if isinstance(page, UserDetailPage):
            fresh = next((u for u in self.policy["users"] if u["uid"] == page.uid), None)
            if fresh is None:
                from .family import guest_user

                guest = guest_user(self.policy)
                if guest is not None and guest["uid"] == page.uid:
                    fresh = guest
            if fresh is None:
                self.nav.pop_to_tag("root")  # the account was removed
            else:
                page.rebuild(fresh)

    # -- shared helpers ------------------------------------------------------

    def toast(self, text: str) -> None:
        self.toasts.add_toast(Adw.Toast(title=text, timeout=4))

    def call(self, work, refresh: bool = True, done_msg: str | None = None,
             on_done=None, after_reload=None) -> None:
        def finished(_result):
            if done_msg:
                self.toast(done_msg)
            if on_done:
                on_done()
            if refresh:
                self.reload(after_reload)
            elif after_reload:
                after_reload()

        run_async(work, finished, lambda e: self.toast(error_text(e)))

    def reload(self, then=None) -> None:
        """Everything the home screen shows, in one trip off the main loop.
        `then` runs once the new policy is on screen."""

        def load():
            client = self.client
            policy = client.get_policy()
            requests = _quiet(client.list_requests, [])
            status = _quiet(client.filter_status, None)
            summary = _quiet(client.activity_summary, {})
            catalog = _quiet(lambda: len(client.list_catalog().get("apps", [])), None)
            return policy, requests, status, summary, catalog

        def on_done(result):
            self.policy, self.requests, self.status, self.summary, self.catalog_count = result
            self.family_page.refresh()
            if self.stack.get_visible_child_name() == "activity":
                self.activity_page.refresh()
            self.refresh_open_detail()
            if then:
                then()

        run_async(load, on_done, lambda e: self.toast(error_text(e)))

    def with_guardian(self, then) -> None:
        """Ask for the guardian password once per session, then call `then(pw)`."""
        if not self.policy["guardian"]["enabled"] or self.guardian_ok:
            then("")
            return

        def proceed(pw: str):
            def on_verified(_r):
                self.guardian_ok = True
                then("")

            run_async(lambda: self.client.verify_guardian(pw), on_verified,
                      lambda e: self.toast(error_text(e)))

        dialog = Adw.AlertDialog(
            heading="Guardian password",
            body="Required once per session to change filter settings.")
        entry = Gtk.PasswordEntry(show_peek_icon=True, hexpand=True)
        dialog.set_extra_child(entry)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("ok", "Continue")
        dialog.set_response_appearance("ok", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("ok")
        dialog.set_close_response("cancel")
        submit_on_enter(dialog, "ok", entry)

        def on_response(_d, response):
            if response == "ok":
                proceed(entry.get_text())

        dialog.connect("response", on_response)
        dialog.present(self)
        entry.grab_focus()


def _quiet(work, default):
    """A secondary read that fails must never keep the home screen from
    loading; the policy is the one thing that has to be there."""
    try:
        return work()
    except Exception:  # noqa: BLE001 - shown as a missing panel, not a crash
        return default


class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)

    def do_activate(self):
        win = self.get_active_window() or Window(application=self)
        win.present()


def main() -> int:
    return App().run(None)


if __name__ == "__main__":
    raise SystemExit(main())
