"""KosherOS Admin — GTK4/libadwaita client of kosherd.

Every mutating call runs in a worker thread (the desktop polkit agent may
prompt, which blocks the call); UI updates hop back via GLib.idle_add.
"""

from __future__ import annotations

import subprocess
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

        self.toasts = Adw.ToastOverlay()
        self.set_content(self.toasts)

        stack = Adw.ViewStack()
        self.profiles_page = ProfilesPage(self)
        self.apps_page = AppsPage(self)
        self.system_page = SystemPage(self)
        stack.add_titled_with_icon(self.profiles_page, "profiles", "Profiles", "system-users-symbolic")
        stack.add_titled_with_icon(self.apps_page, "apps", "Apps", "view-grid-symbolic")
        stack.add_titled_with_icon(self.system_page, "system", "System", "emblem-system-symbolic")

        switcher = Adw.ViewSwitcher(stack=stack, policy=Adw.ViewSwitcherPolicy.WIDE)
        header = Adw.HeaderBar(title_widget=switcher)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.append(header)
        box.append(stack)
        self.toasts.set_child(box)

        self.reload()

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
        """Ask for the guardian password when enabled, then call `then(pw)`."""
        if not self.policy["guardian"]["enabled"]:
            then("")
            return
        dialog = Adw.AlertDialog(heading="Guardian password",
                                 body="A filter change requires the guardian password.")
        entry = Gtk.PasswordEntry(show_peek_icon=True, hexpand=True)
        dialog.set_extra_child(entry)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("ok", "Continue")
        dialog.set_response_appearance("ok", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("ok")

        def on_response(_d, response):
            if response == "ok":
                then(entry.get_text())

        dialog.connect("response", on_response)
        dialog.present(self)


class ProfilesPage(Adw.PreferencesPage):
    def __init__(self, win: Window):
        super().__init__()
        self.win = win
        self.users_group: Adw.PreferencesGroup | None = None

        actions = Adw.PreferencesGroup()
        adopt = Adw.ButtonRow(title="Adopt Existing User…")
        adopt.connect("activated", lambda *_: self._user_dialog(adopt_mode=True))
        create = Adw.ButtonRow(title="Create Child Account…")
        create.connect("activated", lambda *_: self._user_dialog(adopt_mode=False))
        guardian = Adw.ButtonRow(title="Guardian Password…")
        guardian.connect("activated", lambda *_: self._guardian_dialog())
        for row in (adopt, create, guardian):
            actions.add(row)
        self.actions_group = actions

    def refresh(self) -> None:
        if self.users_group is not None:
            self.remove(self.users_group)
            self.remove(self.actions_group)
        group = Adw.PreferencesGroup(title="Profiles")
        g = self.win.policy["guardian"]["enabled"]
        group.set_description(f"Guardian dual-control is {'ON' if g else 'off'}")
        for user in self.win.policy["users"]:
            group.add(self._user_row(user))
        self.users_group = group
        self.add(group)
        self.add(self.actions_group)

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

        wl = Adw.EntryRow(title="Whitelisted domains (comma separated)")
        wl.set_text(", ".join(user.get("whitelist", [])))

        def on_wl(_entry):
            domains = [d.strip() for d in wl.get_text().split(",") if d.strip()]
            self.win.with_guardian(lambda pw: self.win.call(
                lambda: self.win.client.set_whitelist(user["uid"], domains, pw),
                done_msg=f"Whitelist saved for {user['username']}"))

        wl.connect("apply", on_wl)
        wl.set_show_apply_button(True)
        row.add_row(wl)
        return row

    def _user_dialog(self, adopt_mode: bool) -> None:
        title = "Adopt Existing User" if adopt_mode else "Create Child Account"
        dialog = Adw.AlertDialog(heading=title)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        name = Adw.EntryRow(title="Username")
        full = Adw.EntryRow(title="Full name")
        mode = Adw.ComboRow(title="Filter mode",
                            model=Gtk.StringList.new([MODE_LABELS[m] for m in MODES]))
        mode.set_selected(MODES.index("whitelist"))
        group = Adw.PreferencesGroup()
        group.add(name)
        if not adopt_mode:
            group.add(full)
        group.add(mode)
        box.append(group)
        dialog.set_extra_child(box)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("ok", title.split()[0])
        dialog.set_response_appearance("ok", Adw.ResponseAppearance.SUGGESTED)

        def on_response(_d, response):
            if response != "ok":
                return
            username = name.get_text().strip()
            m = MODES[mode.get_selected()]
            if adopt_mode:
                self.win.call(lambda: self.win.client.adopt_user(username, m),
                              done_msg=f"Adopted {username}")
            else:
                self.win.call(
                    lambda: self.win.client.create_child(username, full.get_text().strip() or username, m),
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
    def __init__(self, win: Window):
        super().__init__()
        self.win = win
        self.group: Adw.PreferencesGroup | None = None
        self.refresh()

    def _installed(self) -> set[str]:
        try:
            out = subprocess.run(
                ["flatpak", "list", "--system", "--app", "--columns=application"],
                capture_output=True, text=True, timeout=15).stdout
            return {line.strip() for line in out.splitlines() if line.strip()}
        except Exception:  # noqa: BLE001
            return set()

    def refresh(self) -> None:
        def load():
            return self.win.client.list_catalog(), self._installed()

        def on_done(result):
            catalog, installed = result
            if self.group is not None:
                self.remove(self.group)
            group = Adw.PreferencesGroup(
                title="Approved Apps",
                description="Only apps from the KosherOS catalog can be installed")
            for app in catalog.get("apps", []):
                row = Adw.ActionRow(title=app.get("name", app["ref"]), subtitle=app["ref"])
                ref = app["ref"]
                if ref in installed:
                    btn = Gtk.Button(label="Remove", valign=Gtk.Align.CENTER)
                    btn.add_css_class("destructive-action")
                    btn.connect("clicked", lambda _b, r=ref: self.win.call(
                        lambda: self.win.client.remove_app(r), refresh=False,
                        done_msg=f"Removed {r}") or self.refresh())
                else:
                    btn = Gtk.Button(label="Install", valign=Gtk.Align.CENTER)
                    btn.add_css_class("suggested-action")
                    btn.connect("clicked", lambda _b, r=ref: self.win.call(
                        lambda: self.win.client.install_app(r), refresh=False,
                        done_msg=f"Installed {r}") or self.refresh())
                row.add_suffix(btn)
                group.add(row)
            self.group = group
            self.add(group)

        _run_async(load, on_done, lambda e: self.win.toast(_error_text(e)))


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
