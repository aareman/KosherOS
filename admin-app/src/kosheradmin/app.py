"""KosherOS Admin — GTK4/libadwaita client of kosherd.

Every mutating call runs in a worker thread (the desktop polkit agent may
prompt, which blocks the call); UI updates hop back via GLib.idle_add.
"""

from __future__ import annotations

import threading
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402

from kosherd.client import DaemonClient  # noqa: E402
from kosherd.policy import MODES  # noqa: E402

APP_ID = "org.kosherlinux.Admin"

MODE_LABELS = {
    "none": "No internet",
    "whitelist": "Whitelist only",
    "dnsfilter": "Filtered internet",
    "inspect": "Filtered internet + page rules",
}

MODE_HINTS = {
    "inspect": "Reads web addresses to apply page rules. This decrypts this "
               "user's web traffic on this computer.",
}


def _run_async(work, on_done, on_error) -> None:
    """Run `work()` off the main loop; deliver result/exception on it."""

    def runner():
        try:
            result = work()
        except Exception as e:  # noqa: BLE001 - surfaced to the user as a toast
            GLib.idle_add(on_error, e)
        else:
            GLib.idle_add(on_done, result)

    threading.Thread(target=runner, daemon=True).start()


def _error_text(e: Exception) -> str:
    import re

    msg = str(e)
    msg = re.sub(r"^.*?GDBus\.Error:[\w.]+: ", "", msg)
    return re.sub(r" \(\d+\)$", "", msg)


class Window(Adw.ApplicationWindow):
    def __init__(self, **kwargs):
        super().__init__(**kwargs, title="KosherOS Admin", default_width=760, default_height=640)
        self.client = DaemonClient()
        self.policy: dict = {"users": [], "guardian": {"enabled": False}}
        self.guardian_ok = False

        self.toasts = Adw.ToastOverlay()
        self.set_content(self.toasts)

        stack = Adw.ViewStack()
        self.stack = stack
        self.profiles_page = ProfilesPage(self)
        self.apps_page = AppsPage(self)
        self.system_page = SystemPage(self)
        # Pages must not talk to the daemon before Unlock, or their calls
        # race the unlock prompt.
        stack.add_titled_with_icon(self.profiles_page, "profiles", "Profiles", "system-users-symbolic")
        stack.add_titled_with_icon(self.apps_page, "apps", "Apps", "view-grid-symbolic")
        stack.add_titled_with_icon(self.system_page, "system", "System", "emblem-system-symbolic")

        switcher = Adw.ViewSwitcher(stack=stack, policy=Adw.ViewSwitcherPolicy.WIDE)
        self.header = Adw.HeaderBar(title_widget=switcher)
        lock_btn = Gtk.Button(icon_name="changes-prevent-symbolic",
                              tooltip_text="Lock now")
        lock_btn.connect("clicked", lambda _b: self._lock())
        self.header.pack_end(lock_btn)

        self.content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.content.append(self.header)
        self.content.append(stack)

        # Locked state: one password, then everything works.
        self.lock_view = Adw.StatusPage(
            icon_name="changes-prevent-symbolic",
            title="KosherOS Admin",
            description="Unlock once to manage profiles, filters, and apps.")
        unlock_btn = Gtk.Button(label="Unlock", halign=Gtk.Align.CENTER)
        unlock_btn.add_css_class("suggested-action")
        unlock_btn.add_css_class("pill")
        unlock_btn.connect("clicked", lambda _b: self._unlock())
        self.lock_view.set_child(unlock_btn)

        self.toasts.set_child(self.lock_view)
        self._unlock()  # prompt immediately on open

    def _unlock(self) -> None:
        def on_done(_r):
            self.toasts.set_child(self.content)
            self.reload()
            self.apps_page.refresh()

        _run_async(self.client.unlock, on_done, lambda e: (
            self.toasts.set_child(self.lock_view), self.toast(_error_text(e))))

    def _lock(self) -> None:
        self.guardian_ok = False
        self.toasts.set_child(self.lock_view)
        _run_async(self.client.lock, lambda _r: None, lambda e: self.toast(_error_text(e)))

    # -- shared helpers ------------------------------------------------------

    def toast(self, text: str) -> None:
        self.toasts.add_toast(Adw.Toast(title=text, timeout=4))

    def call(self, work, refresh: bool = True, done_msg: str | None = None) -> None:
        def on_done(_result):
            if done_msg:
                self.toast(done_msg)
            if refresh:
                self.reload()

        _run_async(work, on_done, lambda e: self.toast(_error_text(e)))

    def reload(self) -> None:
        def on_done(policy):
            self.policy = policy
            self.profiles_page.refresh()
            self.system_page.refresh()

        _run_async(self.client.get_policy, on_done, lambda e: self.toast(_error_text(e)))

    def with_guardian(self, then) -> None:
        """Ask for the guardian password once per session, then call `then(pw)`."""
        if not self.policy["guardian"]["enabled"] or self.guardian_ok:
            then("")
            return

        def proceed(pw: str):
            def on_verified(_r):
                self.guardian_ok = True
                then("")

            _run_async(lambda: self.client.verify_guardian(pw), on_verified,
                       lambda e: self.toast(_error_text(e)))

        dialog = Adw.AlertDialog(
            heading="Guardian password",
            body="Required once per session to change filter settings.")
        entry = Gtk.PasswordEntry(show_peek_icon=True, hexpand=True)
        dialog.set_extra_child(entry)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("ok", "Continue")
        dialog.set_response_appearance("ok", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("ok")

        def on_response(_d, response):
            if response == "ok":
                proceed(entry.get_text())

        dialog.connect("response", on_response)
        dialog.present(self)


class WhitelistDialog(Adw.Dialog):
    """Scalable domain list editor: search, add, remove, save."""

    def __init__(self, win: Window, title: str, domains: list[str], on_save):
        super().__init__(title=title, content_width=520, content_height=620)
        self.win = win
        self.on_save = on_save
        self.domains = sorted(domains)

        header = Adw.HeaderBar()
        save = Gtk.Button(label="Save")
        save.add_css_class("suggested-action")
        save.connect("clicked", lambda _b: (on_save(self.domains), self.close()))
        header.pack_end(save)

        self.entry = Gtk.Entry(placeholder_text="example.com  (includes subdomains)",
                               hexpand=True)
        self.entry.connect("activate", lambda _e: self._add())
        add_btn = Gtk.Button(icon_name="list-add-symbolic", tooltip_text="Add domain")
        add_btn.add_css_class("suggested-action")
        add_btn.connect("clicked", lambda _b: self._add())
        add_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6,
                          margin_start=12, margin_end=12, margin_top=12, margin_bottom=6)
        add_box.append(self.entry)
        add_box.append(add_btn)

        self.search = Gtk.SearchEntry(placeholder_text="Search domains",
                                      margin_start=12, margin_end=12, margin_bottom=6)
        self.search.connect("search-changed", lambda _e: self._rebuild())

        self.list_box = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE,
                                    margin_start=12, margin_end=12, margin_bottom=12)
        self.list_box.add_css_class("boxed-list")
        scroller = Gtk.ScrolledWindow(vexpand=True)
        scroller.set_child(self.list_box)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.append(header)
        box.append(add_box)
        box.append(self.search)
        box.append(scroller)
        self.set_child(box)
        self._rebuild()

    def _add(self) -> None:
        text = self.entry.get_text().strip().lower()
        if not text:
            return
        for raw in text.replace(",", " ").split():
            d = raw.strip().removeprefix("https://").removeprefix("http://").split("/")[0]
            if d and d not in self.domains:
                self.domains.append(d)
        self.domains.sort()
        self.entry.set_text("")
        self._rebuild()

    def _rebuild(self) -> None:
        while (child := self.list_box.get_first_child()) is not None:
            self.list_box.remove(child)
        needle = self.search.get_text().strip().lower()
        shown = [d for d in self.domains if needle in d]
        if not shown:
            empty = Adw.ActionRow(
                title="No domains yet" if not self.domains else "No matches",
                subtitle="Add a domain above" if not self.domains else None)
            empty.set_sensitive(False)
            self.list_box.append(empty)
            return
        for d in shown:
            row = Adw.ActionRow(title=d)
            rm = Gtk.Button(icon_name="user-trash-symbolic", valign=Gtk.Align.CENTER,
                            tooltip_text="Remove")
            rm.add_css_class("flat")
            rm.connect("clicked", lambda _b, dom=d: (
                self.domains.remove(dom), self._rebuild()))
            row.add_suffix(rm)
            self.list_box.append(row)


class ProfilesPage(Adw.PreferencesPage):
    def __init__(self, win: Window):
        super().__init__()
        self.win = win
        self.users_group: Adw.PreferencesGroup | None = None
        self.guest_group: Adw.PreferencesGroup | None = None

        actions = Adw.PreferencesGroup()
        adopt = Adw.ButtonRow(title="Adopt Existing User…")
        adopt.connect("activated", lambda *_: self._user_dialog(adopt_mode=True))
        create = Adw.ButtonRow(title="Create User Account…")
        create.connect("activated", lambda *_: self._user_dialog(adopt_mode=False))
        guardian = Adw.ButtonRow(title="Guardian Password…")
        guardian.connect("activated", lambda *_: self._guardian_dialog())
        for row in (adopt, create, guardian):
            actions.add(row)
        self.actions_group = actions

    def refresh(self) -> None:
        if self.users_group is not None:
            self.remove(self.users_group)
            self.remove(self.guest_group)
            self.remove(self.actions_group)
        group = Adw.PreferencesGroup(title="Profiles")
        g = self.win.policy["guardian"]["enabled"]
        group.set_description(f"Guardian dual-control is {'ON' if g else 'off'}")
        for user in self.win.policy["users"]:
            group.add(self._user_row(user))
        self.users_group = group
        self.add(group)
        self.guest_group = self._build_guest_group()
        self.add(self.guest_group)
        self.add(self.actions_group)

    def _build_guest_group(self) -> Adw.PreferencesGroup:
        guest = self.win.policy.get("guest", {"enabled": False})
        group = Adw.PreferencesGroup(
            title="Guest Session",
            description="Passwordless account; all guest data is erased at sign-out")

        mode = guest.get("mode", "whitelist")
        wl_domains = guest.get("whitelist", [])

        def push(enabled, new_mode, domains):
            self.win.with_guardian(lambda pw: self.win.call(
                lambda: self.win.client.set_guest_config(enabled, new_mode, domains, pw),
                done_msg="Guest settings saved"))

        switch = Adw.SwitchRow(title="Enable guest account", active=guest["enabled"])
        switch.connect("notify::active",
                       lambda s, _p: s.get_active() != guest["enabled"] and
                       push(s.get_active(), mode, wl_domains))
        group.add(switch)

        mode_row = Adw.ComboRow(title="Guest filter mode",
                                model=Gtk.StringList.new([MODE_LABELS[m] for m in MODES]))
        mode_row.set_selected(MODES.index(mode))
        mode_row.connect("notify::selected",
                         lambda c, _p: MODES[c.get_selected()] != mode and
                         push(guest["enabled"], MODES[c.get_selected()], wl_domains))
        group.add(mode_row)

        wl_row = Adw.ActionRow(
            title="Guest whitelist",
            subtitle=f"{len(wl_domains)} domain{'s' if len(wl_domains) != 1 else ''}",
            activatable=True)
        wl_row.add_suffix(Gtk.Image(icon_name="go-next-symbolic"))
        wl_row.connect("activated", lambda _r: WhitelistDialog(
            self.win, "Whitelist — Guest", wl_domains,
            lambda new: push(guest["enabled"], mode, new)).present(self.win))
        if mode != "whitelist":
            wl_row.set_sensitive(False)
            wl_row.set_subtitle("Only used in Whitelist only mode")
        group.add(wl_row)
        return group

    def _user_row(self, user: dict) -> Adw.ExpanderRow:
        row = Adw.ExpanderRow(title=user["username"])
        badges = [MODE_LABELS[user["mode"]]]
        if user.get("admin"):
            badges.append("admin")
        row.set_subtitle(" · ".join(badges))

        mode_row = Adw.ComboRow(title="Filter mode",
                                model=Gtk.StringList.new([MODE_LABELS[m] for m in MODES]))
        mode_row.set_selected(MODES.index(user["mode"]))

        def on_mode(combo, _p):
            new_mode = MODES[combo.get_selected()]
            if new_mode == user["mode"]:
                return
            self.win.with_guardian(lambda pw: self.win.call(
                lambda: self.win.client.set_filter_mode(user["uid"], new_mode, pw),
                done_msg=f"{user['username']} → {MODE_LABELS[new_mode]}"))

        mode_row.connect("notify::selected", on_mode)
        row.add_row(mode_row)

        domains = user.get("whitelist", [])
        wl_row = Adw.ActionRow(title="Whitelist",
                               subtitle=f"{len(domains)} domain{'s' if len(domains) != 1 else ''}")
        wl_row.set_activatable(True)
        wl_row.add_suffix(Gtk.Image(icon_name="go-next-symbolic"))
        wl_row.connect("activated", lambda _r: WhitelistDialog(
            self.win, f"Whitelist — {user['username']}", domains,
            lambda new: self.win.with_guardian(lambda pw: self.win.call(
                lambda: self.win.client.set_whitelist(user["uid"], new, pw),
                done_msg=f"Whitelist saved for {user['username']}"))).present(self.win))
        row.add_row(wl_row)
        if user["mode"] != "whitelist":
            wl_row.set_sensitive(False)
            wl_row.set_subtitle("Only used in Whitelist only mode")

        rules = user.get("rules", [])
        rules_row = Adw.ActionRow(
            title="Page rules",
            subtitle=f"{len(rules)} rule{'s' if len(rules) != 1 else ''}",
            activatable=True)
        rules_row.add_suffix(Gtk.Image(icon_name="go-next-symbolic"))
        rules_row.connect("activated", lambda _r: RulesDialog(
            self.win, f"Page rules — {user['username']}", rules,
            lambda new: self.win.with_guardian(lambda pw: self.win.call(
                lambda: self.win.client.set_url_rules(user["uid"], new, pw),
                done_msg=f"Page rules saved for {user['username']}"))).present(self.win))
        if user["mode"] != "inspect":
            rules_row.set_sensitive(False)
            rules_row.set_subtitle("Only used in “Filtered internet + page rules” mode")
        row.add_row(rules_row)

        installs = Adw.SwitchRow(title="Can install approved apps",
                                 subtitle="Only apps on the approved list, from the KosherOS Store",
                                 active=user.get("can_install_apps", True))
        installs.connect("notify::active", lambda s, _p: (
            s.get_active() != user.get("can_install_apps", True) and self.win.call(
                lambda: self.win.client.set_user_can_install(user["uid"], s.get_active()),
                done_msg=f"App installs {'enabled' if s.get_active() else 'disabled'} for {user['username']}")))
        row.add_row(installs)
        return row

    def _unmanaged_users(self) -> list[str]:
        import pwd

        managed = {u["uid"] for u in self.win.policy["users"]}
        guest_uid = self.win.policy.get("guest", {}).get("uid")
        return sorted(
            p.pw_name for p in pwd.getpwall()
            if 1000 <= p.pw_uid < 65000
            and p.pw_uid not in managed
            and p.pw_uid != guest_uid
        )

    def _user_dialog(self, adopt_mode: bool) -> None:
        title = "Adopt Existing User" if adopt_mode else "Create User Account"
        dialog = Adw.AlertDialog(heading=title)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        group = Adw.PreferencesGroup()

        candidates: list[str] = []
        if adopt_mode:
            candidates = self._unmanaged_users()
            if not candidates:
                self.win.toast("Every existing account is already managed")
                return
            name_row = Adw.ComboRow(title="Account",
                                    model=Gtk.StringList.new(candidates))
            group.add(name_row)
        else:
            name = Adw.EntryRow(title="Username")
            full = Adw.EntryRow(title="Full name")
            group.add(name)
            group.add(full)

        mode = Adw.ComboRow(title="Filter mode",
                            model=Gtk.StringList.new([MODE_LABELS[m] for m in MODES]))
        mode.set_selected(MODES.index("whitelist"))
        group.add(mode)
        box.append(group)
        dialog.set_extra_child(box)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("ok", title.split()[0])
        dialog.set_response_appearance("ok", Adw.ResponseAppearance.SUGGESTED)

        def on_response(_d, response):
            if response != "ok":
                return
            m = MODES[mode.get_selected()]
            if adopt_mode:
                username = candidates[name_row.get_selected()]
                self.win.call(lambda: self.win.client.adopt_user(username, m),
                              done_msg=f"Adopted {username}")
            else:
                username = name.get_text().strip()
                self.win.call(
                    lambda: self.win.client.create_user(username, full.get_text().strip() or username, m),
                    done_msg=f"Created {username}")

        dialog.connect("response", on_response)
        dialog.present(self.win)

    def _guardian_dialog(self) -> None:
        enabled = self.win.policy["guardian"]["enabled"]
        dialog = Adw.AlertDialog(
            heading="Guardian password",
            body="A second password (e.g. the other spouse's) required for any filter change.")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        old = Gtk.PasswordEntry(show_peek_icon=True)
        new = Gtk.PasswordEntry(show_peek_icon=True)
        if enabled:
            box.append(Gtk.Label(label="Current password", xalign=0))
            box.append(old)
        box.append(Gtk.Label(label="New password", xalign=0))
        box.append(new)
        dialog.set_extra_child(box)
        dialog.add_response("cancel", "Cancel")
        if enabled:
            dialog.add_response("disable", "Disable Guardian")
            dialog.set_response_appearance("disable", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.add_response("ok", "Set Password")
        dialog.set_response_appearance("ok", Adw.ResponseAppearance.SUGGESTED)

        def on_response(_d, response):
            if response == "ok":
                self.win.call(lambda: self.win.client.set_guardian_password(old.get_text(), new.get_text()),
                              done_msg="Guardian enabled")
            elif response == "disable":
                self.win.call(lambda: self.win.client.disable_guardian(old.get_text()),
                              done_msg="Guardian disabled")

        dialog.connect("response", on_response)
        dialog.present(self.win)


class AppsPage(Adw.PreferencesPage):
    """Curates the approved-app list. Installing is the Store's job."""

    def __init__(self, win: Window):
        super().__init__()
        self.win = win
        self.group: Adw.PreferencesGroup | None = None
        self.search_group: Adw.PreferencesGroup | None = None
        self.approved: list[dict] = []
        self.installed: set[str] = set()
        # No daemon calls here: Window refreshes this page after Unlock.

    def refresh(self) -> None:
        def load():
            return (self.win.client.list_catalog().get("apps", []),
                    set(self.win.client.list_installed()))

        def on_done(result):
            self.approved, self.installed = result
            self._render_approved()
            if self.search_group is None:
                self._build_search()

        _run_async(load, on_done, lambda e: self.win.toast(_error_text(e)))

    def _render_approved(self) -> None:
        if self.group is not None:
            self.remove(self.group)
        group = Adw.PreferencesGroup(
            title=f"Approved apps ({len(self.approved)})",
            description="Anyone using this computer may install these from the "
                        "KosherOS Store. Nothing else can be installed.")
        if not self.approved:
            row = Adw.ActionRow(title="No apps approved yet",
                                subtitle="Search below to approve apps")
            row.set_sensitive(False)
            group.add(row)
        for app in self.approved:
            ref = app["ref"]
            row = Adw.ActionRow(title=app.get("name", ref), subtitle=ref)
            if ref in self.installed:
                badge = Gtk.Label(label="Installed", valign=Gtk.Align.CENTER)
                badge.add_css_class("dim-label")
                row.add_suffix(badge)
            btn = Gtk.Button(label="Unapprove", valign=Gtk.Align.CENTER)
            btn.add_css_class("destructive-action")
            btn.set_tooltip_text("Remove from the approved list"
                                 + (" (does not uninstall it)" if ref in self.installed else ""))
            btn.connect("clicked", lambda _b, r=ref: self.win.call(
                lambda: self.win.client.unapprove_app(r), refresh=False,
                done_msg=f"{r} is no longer approved") or GLib.timeout_add(
                    400, lambda: (self.refresh(), False)[1]))
            row.add_suffix(btn)
            group.add(row)
        self.group = group
        self.add(group)
        if self.search_group is not None:
            self.remove(self.search_group)
            self.add(self.search_group)

    def _build_search(self) -> None:
        group = Adw.PreferencesGroup(
            title="Add apps",
            description="Search everything available, then approve what you "
                        "want people on this computer to be able to install.")
        entry = Adw.EntryRow(title="Search all apps")
        entry.set_show_apply_button(True)
        self.results_box = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE,
                                       margin_top=6)
        self.results_box.add_css_class("boxed-list")
        group.add(entry)
        group.add(self.results_box)

        def do_search(_w):
            query = entry.get_text().strip()
            self._show_results_message("Searching…")

            def on_done(results):
                approved = {a["ref"] for a in self.approved}
                shown = [r for r in results if r["ref"] not in approved][:40]
                if not shown:
                    self._show_results_message("No matches")
                    return
                self._clear_results()
                for app in shown:
                    row = Adw.ActionRow(title=app["name"],
                                        subtitle=app.get("summary") or app["ref"])
                    btn = Gtk.Button(label="Approve", valign=Gtk.Align.CENTER)
                    btn.add_css_class("suggested-action")
                    btn.connect("clicked", lambda _b, a=app: self.win.call(
                        lambda: self.win.client.approve_app(
                            a["ref"], a["name"], a.get("summary", "")),
                        refresh=False, done_msg=f"{a['name']} approved")
                        or GLib.timeout_add(400, lambda: (self.refresh(), False)[1]))
                    row.add_suffix(btn)
                    self.results_box.append(row)

            _run_async(lambda: self.win.client.search_apps(query), on_done,
                       lambda e: self._show_results_message(_error_text(e)))

        entry.connect("apply", do_search)
        entry.connect("entry-activated", do_search)
        self.search_group = group
        self.add(group)

    def _clear_results(self) -> None:
        while (child := self.results_box.get_first_child()) is not None:
            self.results_box.remove(child)

    def _show_results_message(self, text: str) -> None:
        self._clear_results()
        row = Adw.ActionRow(title=text)
        row.set_sensitive(False)
        self.results_box.append(row)


class SystemPage(Adw.PreferencesPage):
    def __init__(self, win: Window):
        super().__init__()
        self.win = win
        group = Adw.PreferencesGroup(title="System")

        pretty = "KosherOS"
        for line in Path("/etc/os-release").read_text().splitlines():
            if line.startswith("PRETTY_NAME="):
                pretty = line.split("=", 1)[1].strip('"')
        self.about_row = Adw.ActionRow(title="Operating system", subtitle=pretty)
        group.add(self.about_row)

        self.status_row = Adw.ActionRow(title="Updates", subtitle="—")
        check = Gtk.Button(label="Check", valign=Gtk.Align.CENTER)
        check.connect("clicked", self._check)
        apply_btn = Gtk.Button(label="Update Now", valign=Gtk.Align.CENTER)
        apply_btn.add_css_class("suggested-action")
        apply_btn.connect("clicked", self._apply)
        self.status_row.add_suffix(check)
        self.status_row.add_suffix(apply_btn)
        group.add(self.status_row)
        self.add(group)

    def refresh(self) -> None:
        pass

    def _check(self, _b) -> None:
        self.status_row.set_subtitle("Checking…")

        def on_done(status):
            first = next((line for line in status.splitlines() if line.strip()), "Up to date")
            self.status_row.set_subtitle(first[:120])

        _run_async(self.win.client.check_update, on_done,
                   lambda e: self.status_row.set_subtitle(_error_text(e)))

    def _apply(self, _b) -> None:
        self.status_row.set_subtitle("Updating (staged on reboot when done)…")
        self.win.call(self.win.client.apply_update, refresh=False,
                      done_msg="Update staged — reboot to apply")


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
