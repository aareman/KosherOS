"""KosherOS Admin — GTK4/libadwaita client of kosherd.

The window: unlocked by the admin's own sign-in, then a sidebar down the left with
everywhere the app goes — the family, each on a full page of their own,
then the activity feed and this computer's protection, apps and updates —
and the page itself beside it. Above every page, the two banners that
speak only when they must: requests waiting, and the filter's health. Every
mutating call runs in a worker thread (the desktop polkit agent may
prompt, which blocks the call); UI updates hop back via GLib.idle_add.

The label tables the daemon's tests read live in labels.py; the sidebar in
sidebar.py; the pages in family.py, feed.py, computer.py and detail.py;
the dialogs in dialogs.py.
"""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, Gtk  # noqa: E402

from kosherd.client import DaemonClient  # noqa: E402

from . import labels  # noqa: E402
from .common import error_text, load_css, run_async, submit_on_enter  # noqa: E402
from .computer import AppsPage, ProtectionPage, UpdatesPage, health_rows  # noqa: E402
from .detail import UserDetailPage  # noqa: E402
from .dialogs import RequestsDialog, add_person_dialog  # noqa: E402
from .feed import ActivityPage  # noqa: E402
from .sidebar import Sidebar  # noqa: E402

APP_ID = "org.kosherlinux.Admin"

# Re-exported so tests and older imports keep one place to look.
EDITABLE_LISTS = labels.EDITABLE_LISTS


class Window(Adw.ApplicationWindow):
    def __init__(self, **kwargs):
        super().__init__(**kwargs, title="KosherOS Admin", default_width=1120,
                         default_height=740)
        load_css()
        self.client = DaemonClient()
        self.policy: dict = {"users": [], "guardian": {"enabled": False}}
        self.requests: list[dict] = []
        self.status: dict | None = None
        self.summary: dict = {}
        self.time_usage: dict = {}
        self.catalog_count: int | None = None
        self.update_state: str | None = None
        self.deployment: dict | None = None
        self.guardian_ok = False
        self.destination = "activity"
        self._landed = False

        self.toasts = Adw.ToastOverlay()
        self.set_content(self.toasts)

        # Pages must not talk to the daemon before Unlock, or their calls
        # race the unlock prompt.
        self.activity_page = ActivityPage(self)

        # The banners: above every page, not on one of them. The user asked
        # for "a blue notification at the top: X requests waiting for you,
        # click to deal with", and a problem with the filter is the one
        # amber thing that must not wait for a particular page to be opened.
        self.requests_banner = Adw.Banner(button_label="Deal with them")
        self.requests_banner.add_css_class("requests-banner")
        self.requests_banner.connect("button-clicked", lambda _b: self.show_requests())
        self.health_banner = Adw.Banner(button_label="Details")
        self.health_banner.add_css_class("health-banner")
        self.health_banner.connect("button-clicked", lambda _b: self.go_to("protection"))

        # A navigation view inside the content pane, so configuring one
        # person gets a full page of its own — with the sidebar still
        # there — instead of an expander squeezed into the list.
        self.nav = Adw.NavigationView(vexpand=True)
        column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        column.append(self.requests_banner)
        column.append(self.health_banner)
        column.append(self.nav)
        self.sidebar = Sidebar(self)
        self.split = Adw.NavigationSplitView(
            sidebar=self.sidebar,
            content=Adw.NavigationPage(child=column, title="KosherOS Admin"),
            min_sidebar_width=210, max_sidebar_width=280,
            sidebar_width_fraction=0.24)
        self.content = self.split

        # Narrow windows get one pane at a time rather than a squeezed board.
        breakpoint_ = Adw.Breakpoint.new(
            Adw.BreakpointCondition.parse("max-width: 700sp"))
        breakpoint_.add_setter(self.split, "collapsed", True)
        self.add_breakpoint(breakpoint_)

        for name, adopt in (("create-user", False), ("adopt-user", True)):
            action = Gio.SimpleAction(name=name)
            action.connect("activate", lambda _a, _p, a=adopt: add_person_dialog(self, a))
            self.add_action(action)

        self.go_to("activity")

        # Locked state. An administrator's own session unlocks without a
        # prompt; any other account is asked for an administrator's password
        # once, and kosherd keeps the session for the sitting.
        self.lock_view = Adw.StatusPage(
            icon_name="changes-prevent-symbolic",
            title="KosherOS Admin",
            description="Unlock to see the family, answer requests and change settings.")
        unlock_btn = Gtk.Button(label="Unlock", halign=Gtk.Align.CENTER)
        unlock_btn.add_css_class("suggested-action")
        unlock_btn.add_css_class("pill")
        unlock_btn.connect("clicked", lambda _b: self._unlock())
        self.lock_view.set_child(unlock_btn)

        self.toasts.set_child(self.lock_view)
        self._unlock()  # prompt immediately on open

    def _not_an_admin(self) -> bool:
        """True when this account is not in kosher-admin.

        An administrator's own session is never asked for a password (the
        polkit rules answer yes). On any other account, Unlock brings up the
        desktop's prompt for an administrator's password, which is kept for
        the sitting — so the lock screen says whose password it wants.
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
                "This account is not an administrator of this computer. To see "
                "the family or change settings here, unlock with an "
                "administrator's password.")

        def on_done(_r):
            self.toasts.set_child(self.content)
            self.reload()

        run_async(self.client.unlock, on_done, lambda e: (
            self.toasts.set_child(self.lock_view), self.toast(error_text(e))))

    def lock(self) -> None:
        self.guardian_ok = False
        self.toasts.set_child(self.lock_view)
        run_async(self.client.lock, lambda _r: None, lambda e: self.toast(error_text(e)))

    # -- navigation ------------------------------------------------------------------

    def go_to(self, key: str) -> None:
        """Show one of the sidebar's destinations, replacing whatever the
        content pane was showing. Every destination is a replacement, never
        a push: the sidebar is the way back, so there is nothing to go back
        to and no back button to promise it."""
        self.destination = key
        self.nav.replace([self._destination_page(key)])
        self.sidebar.select(key)
        if key == "activity":
            self.activity_page.refresh()
        if self.split.get_collapsed():
            self.split.set_show_content(True)

    def _destination_page(self, key: str) -> Adw.NavigationPage:
        """The page for a sidebar row.

        The board and the feed are long-lived (they hold a selection and a
        scroll position); everything else is built fresh, since a person's
        page and the computer's three pages all read something on the way
        in.
        """
        if key == "activity":
            if not hasattr(self, "_activity_nav_page"):
                view = Adw.ToolbarView()
                # Who and when live in the header, so the feed keeps the
                # whole width and the app has one sidebar, not two.
                header = Adw.HeaderBar(title_widget=self.activity_page.range)
                header.pack_start(self.activity_page.who)
                view.add_top_bar(header)
                view.set_content(self.activity_page)
                self._activity_nav_page = Adw.NavigationPage(
                    child=view, title="Activity", tag="activity")
            return self._activity_nav_page
        if key == "guest" or key.startswith("user-"):
            return self._person_page(key)
        page = {"protection": ProtectionPage, "apps": AppsPage,
                "updates": UpdatesPage}[key](self)
        self.open_computer_page = page
        return page

    def _person_page(self, key: str) -> Adw.NavigationPage:
        """One account's page, or — for a guest that is switched off — the
        page that turns it on, since there is nothing yet to configure."""
        from .family import GuestPage, guest_user

        if key == "guest":
            guest = guest_user(self.policy)
            return UserDetailPage(self, guest) if guest else GuestPage(self)
        uid = int(key.removeprefix("user-"))
        user = next((u for u in self.policy.get("users", []) if u["uid"] == uid), None)
        if user is None:  # removed while it was open
            return self._destination_page("overview")
        return UserDetailPage(self, user)

    def push(self, page: Adw.NavigationPage) -> None:
        self.nav.push(page)

    def pop_to_root(self) -> None:
        """Back out of anything pushed on top of the current destination."""
        stack = self.nav.get_navigation_stack()
        if stack.get_n_items():
            self.nav.pop_to_page(stack.get_item(0))

    def open_user_detail(self, user: dict) -> None:
        from .sidebar import user_key

        self.go_to(user_key(user))

    def home_key(self) -> str:
        """Where the app opens: the first person, since the people are what
        an admin came for; the activity feed when there is nobody yet."""
        users = self.policy.get("users") or []
        return f"user-{users[0]['uid']}" if users else "activity"

    def go_home(self) -> None:
        self.go_to(self.home_key())

    def show_requests(self) -> RequestsDialog:
        dialog = RequestsDialog(self, self.requests)
        dialog.present(self)
        return dialog

    def refresh_banners(self) -> None:
        n = len(self.requests or [])
        if n:
            self.requests_banner.set_title(f"{labels.plural(n, 'request')} waiting for you")
        self.requests_banner.set_revealed(bool(n))
        problems = [r for r in health_rows(self.status) if not r[2]]
        if problems:
            self.health_banner.set_title(problems[0][0])
        self.health_banner.set_revealed(bool(problems))

    def show_activity(self, uid: int) -> None:
        """Jump to the activity page, narrowed to one person."""
        self.go_to("activity")
        self.activity_page.show_user(uid)

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
                self.go_home()  # the account was removed
            else:
                page.rebuild(fresh)
        elif hasattr(page, "refresh") and page is getattr(self, "open_computer_page", None):
            page.refresh()

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
            time_usage = _quiet(client.time_usage, {})
            catalog = _quiet(lambda: len(client.list_catalog().get("apps", [])), None)
            # Once only: bootc takes a moment to answer, and the version
            # this computer is running does not change while it runs.
            deployment = self.deployment if self.deployment is not None \
                else _quiet(client.deployment_status, {})
            return policy, requests, status, summary, time_usage, catalog, deployment

        def on_done(result):
            (self.policy, self.requests, self.status, self.summary, self.time_usage,
             self.catalog_count, self.deployment) = result
            self.refresh_banners()
            self.sidebar.refresh()
            if not self._landed:
                # The first load: open on the first person rather than the
                # placeholder the window started on.
                self._landed = True
                self.go_home()
            elif self.destination == "activity":
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
