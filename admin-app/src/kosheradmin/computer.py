"""This computer: the settings that are not about one person.

The sidebar's second section. Four pages, each a real page rather than a
tile with two words on it:

  Protection — is the filter actually working, what it blocks for
               everyone, and the guardian password that guards the answer
  Apps       — which apps this computer may install at all
  Updates    — what version it runs, and the way back if one goes wrong
  Advanced   — Tailscale and Syncthing, off until someone turns them on

"Applies to everyone" is the honest heading for the middle group. Ad
blocking is rendered into the machine's own resolvers, the word lists are
one list per machine, and neither can be answered differently per account
— so they belong here, with the reason written next to them, rather than
on a person's page where they would look per-person.
"""

from __future__ import annotations

import time
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

from . import labels  # noqa: E402
from .common import (banner_slot, clear, confirm, error_text, pointer_cursors,  # noqa: E501
                     run_async, tag)  # noqa: E402
from .dialogs import ListEditDialog, boot_menu_dialog, guardian_dialog  # noqa: E402


def health_rows(status: dict | None) -> list[tuple[str, str, bool]]:
    """(title, body, ok) for everything the filter status has to say.

    A filter that has quietly stopped doing something is worse than one
    that never did it, because the family is relying on it — and the
    version of this that says nothing teaches people that blank pictures
    mean the computer is broken. So there is always at least one row, and
    when everything is fine it says so, with the time it was checked.
    """
    import time

    if status is None:
        return [("Could not check the filter",
                 "The filter service did not respond. Try again.", False)]
    rows = []
    state = status.get("pictures")
    if state in labels.PICTURE_STATE:
        title, body = labels.PICTURE_STATE[state]
        rows.append((title, body.format(ms=status.get("detect_ms") or "?"), False))
    for service in status.get("degraded", []):
        rows.append(("Part of the filter is not running",
                     f"{service} should be running on this computer and is not. "
                     "Restarting the computer usually fixes it.", False))
    for problem in status.get("problems", []):
        rows.append(("A filter list is missing or damaged",
                     f"{problem[0].upper()}{problem[1:]}.", False))
    checked = time.strftime("%H:%M")
    if rows:
        running = [s for s, state in (status.get("services") or {}).items()
                   if state == "active"]
        if running:
            rows.append(("Everything else is running",
                         f"{len(running)} of {len(status.get('services') or {})} "
                         f"filter services are up. Checked at {checked}.", True))
    else:
        rows.append(("The filter is running",
                     "Web filter, DNS filter and search are all up, and every "
                     f"list loaded. Checked at {checked}.", True))
    return rows


def health_problems(status: dict | None) -> int:
    """How many of the health rows are not fine: the sidebar's amber badge."""
    return sum(1 for _title, _body, ok in health_rows(status) if not ok)


def health_summary(status: dict | None) -> tuple[str, bool]:
    """Two words for the health, and whether it is fine."""
    problems = health_problems(status)
    if not problems:
        return "Running", True
    if problems == 1:
        return "1 problem", False
    return f"{problems} problems", False


class _Page(Adw.NavigationPage):
    """A page with a header bar and a preferences page inside it."""

    def __init__(self, win, title: str, tag: str):
        super().__init__(title=title, tag=tag)
        self.win = win
        self.prefs = Adw.PreferencesPage()
        self.header = Adw.HeaderBar()
        self.banner_slot = banner_slot()
        view = Adw.ToolbarView()
        view.add_top_bar(self.header)
        view.add_top_bar(self.banner_slot)
        view.set_content(self.prefs)
        self.set_child(view)
        # Subclasses fill self.prefs in their __init__; the hand cursor is
        # applied once the page is shown, when the rows exist.
        self.connect("shown", lambda _p: pointer_cursors(self))


class ProtectionPage(_Page):
    """What protects everyone, and whether it is working.

    The health used to live behind a banner and a dialog, which meant the
    only way to see a healthy filter was to have an unhealthy one. Here it
    is the first thing on the page, whatever it says.
    """

    def __init__(self, win):
        super().__init__(win, "Protection", "protection")
        self.health_group = Adw.PreferencesGroup(title="Filter status")
        check = Gtk.Button(label="Check again", valign=Gtk.Align.CENTER)
        check.connect("clicked", lambda _b: self._check())
        self.health_group.set_header_suffix(check)
        self.health_rows_box = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.health_rows_box.add_css_class("boxed-list")
        self.health_group.add(self.health_rows_box)
        self.prefs.add(self.health_group)

        everyone = Adw.PreferencesGroup(
            title="Applies to everyone",
            description=("These settings apply to all accounts, including guests, in every browser and"
                " app."))
        self.ads_row = Adw.SwitchRow(
            title="Block ads and trackers",
            subtitle="Blocks known ad and tracking domains for everyone on this computer.",
            subtitle_lines=4,
            active=win.policy.get("adblock", {}).get("enabled", True))
        self.ads_row.add_css_class("blocking")
        self.ads_row.connect("notify::active", self._on_ads)
        everyone.add(self.ads_row)
        for name, title, description, noun in labels.EDITABLE_LISTS:
            row = Adw.ActionRow(title=title, subtitle=description,
                                subtitle_lines=3, activatable=True)
            row.add_suffix(Gtk.Image(icon_name="go-next-symbolic"))
            row.connect("activated", lambda _r, n=name, t=title, d=description,
                        u=noun: ListEditDialog(win, n, t, d, u).present(win))
            everyone.add(row)
        self.prefs.add(everyone)

        locking = Adw.PreferencesGroup(
            title="Administrator access",
            description=("Only administrators can change these settings. A guardian password adds an "
                "extra check before reducing filter restrictions."))
        self.guardian_row = Adw.ActionRow(
            title="Guardian password", subtitle="—", activatable=True)
        self.guardian_row.add_suffix(Gtk.Image(icon_name="go-next-symbolic"))
        self.guardian_row.connect("activated", lambda _r: guardian_dialog(win))
        locking.add(self.guardian_row)
        self.boot_row = Adw.ActionRow(title="Boot menu password", subtitle="Checking…",
                                      activatable=True, subtitle_lines=3)
        self.boot_row.add_suffix(Gtk.Image(icon_name="go-next-symbolic"))
        self.boot_row.connect("activated", lambda _r: boot_menu_dialog(win))
        locking.add(self.boot_row)
        self.prefs.add(locking)

        self.refresh()

    def refresh(self) -> None:
        clear(self.health_rows_box)
        for title, body, ok in health_rows(self.win.status):
            row = Adw.ActionRow(title=title, subtitle=body, subtitle_lines=5)
            icon = Gtk.Image(icon_name="emblem-ok-symbolic" if ok
                             else "dialog-warning-symbolic")
            icon.add_css_class("success" if ok else "warning")
            row.add_prefix(icon)
            self.health_rows_box.append(row)

        wanted = self.win.policy.get("adblock", {}).get("enabled", True)
        if self.ads_row.get_active() != wanted:
            self.ads_row.handler_block_by_func(self._on_ads)
            self.ads_row.set_active(wanted)
            self.ads_row.handler_unblock_by_func(self._on_ads)

        def show_boot(protected: bool) -> None:
            self.boot_row.set_subtitle(
                "Set. Editing the boot menu asks for it; starting normally never does."
                if protected else
                "Not set. Anyone at the keyboard can edit the boot menu to start the "
                "computer without filtering. Set one.")

        run_async(self.win.client.boot_menu_protected, show_boot,
                  lambda e: self.boot_row.set_subtitle(f"Could not read: {error_text(e)}"))

        on = self.win.policy.get("guardian", {}).get("enabled", False)
        self.guardian_row.set_subtitle(
            "Enabled. Required once per session to reduce filtering restrictions."
            if on else ("Disabled. Administrators can change filtering without an additional "
                "password."))

    def _check(self) -> None:
        self.win.toast("Checking the filter…")
        self.win.reload()

    def _on_ads(self, switch, _param) -> None:
        wanted = switch.get_active()
        if wanted == self.win.policy.get("adblock", {}).get("enabled", True):
            return
        if wanted:
            self.win.call(lambda: self.win.client.set_adblock(True),
                          done_msg="Ads and trackers are blocked for everyone")
            return
        # Switching OFF is the guardian-gated direction.
        self.win.with_guardian(lambda pw: self.win.call(
            lambda: self.win.client.set_adblock(False, pw),
            done_msg="Ad blocking is off"))


class AdvancedPage(_Page):
    """Tailscale and Syncthing, off until an administrator turns them on (#220).

    Most families use neither, so neither runs on a new install. Turning
    Tailscale on starts its service and shows the sign-in below the
    switch; turning Syncthing on lets it be installed from the Store and
    allowed on an account's Apps tab.
    """

    def __init__(self, win):
        super().__init__(win, "Advanced", "advanced")
        switches = Adw.PreferencesGroup(
            title="Extra features",
            description="These are off unless you turn them on here.")
        self.tailscale_row = Adw.SwitchRow(
            title="Tailscale",
            subtitle=("Lets this computer join your tailnet, so the people who use it can "
                      "reach your other machines by name."),
            subtitle_lines=3)
        self.tailscale_row.connect("notify::active", self._on_tailscale)
        switches.add(self.tailscale_row)
        self.syncthing_row = Adw.SwitchRow(
            title="Syncthing",
            subtitle=("Keeps folders the same on a person's computers and phone. Once it is "
                      "on, you allow it for each account on that account's Apps tab."),
            subtitle_lines=3)
        self.syncthing_row.connect("notify::active", self._on_syncthing)
        switches.add(self.syncthing_row)
        self.prefs.add(switches)

        self.tailscale = TailscaleGroup(win)
        self.prefs.add(self.tailscale)
        self.refresh()

    def _advanced(self) -> dict:
        return self.win.policy.get("advanced") or {}

    def _set_row(self, row: Adw.SwitchRow, handler, wanted: bool) -> None:
        if row.get_active() != wanted:
            row.handler_block_by_func(handler)
            row.set_active(wanted)
            row.handler_unblock_by_func(handler)

    def refresh(self) -> None:
        on = self._advanced()
        self._set_row(self.tailscale_row, self._on_tailscale, bool(on.get("tailscale")))
        self._set_row(self.syncthing_row, self._on_syncthing, bool(on.get("syncthing")))
        self.tailscale.set_visible(bool(on.get("tailscale")))
        if on.get("tailscale"):
            self.tailscale.refresh()

    def _on_tailscale(self, row, _param) -> None:
        wanted = row.get_active()
        if wanted == bool(self._advanced().get("tailscale")):
            return
        if wanted:
            self._switch(lambda pw: self.win.client.set_tailscale_enabled(True, pw),
                         "Tailscale is on. Sign in below.")
            return
        dialog = confirm(self.win, "Turn off Tailscale?",
                         "This computer leaves your tailnet until Tailscale is turned on "
                         "again. It stays signed in, so turning it back on reconnects.",
                         "Turn Off",
                         lambda: self._switch(
                             lambda pw: self.win.client.set_tailscale_enabled(False, pw),
                             "Tailscale is off"))
        dialog.connect("response", lambda _d, r: r != "yes" and self.refresh())

    def _on_syncthing(self, row, _param) -> None:
        wanted = row.get_active()
        if wanted == bool(self._advanced().get("syncthing")):
            return
        if wanted:
            self._switch(lambda pw: self.win.client.set_syncthing_enabled(True, pw),
                         "Syncthing is on")
            return
        dialog = confirm(self.win, "Turn off Syncthing?",
                         "Syncthing stops for every account, and the Store stops offering it. "
                         "Synced folders stay where they are.",
                         "Turn Off",
                         lambda: self._switch(
                             lambda pw: self.win.client.set_syncthing_enabled(False, pw),
                             "Syncthing is off"))
        dialog.connect("response", lambda _d, r: r != "yes" and self.refresh())

    def _switch(self, work, done_msg: str) -> None:
        # Refused or cancelled, the policy is as it was: the switch goes
        # back to match it.
        def failed(e) -> None:
            self.win.toast(error_text(e))
            self.refresh()

        self.win.with_guardian(lambda pw: run_async(
            lambda: work(pw),
            lambda _r: (self.win.toast(done_msg), self.win.reload()),
            failed), on_cancel=self.refresh)


class TailscaleGroup(Adw.PreferencesGroup):
    """This computer on the family's tailnet (#30).

    Signing in happens in a browser: the daemon starts it and hands back an
    address, the page shows it with Open and Copy (the administrator may
    rather use a phone), and then checks every few seconds until the
    sign-in is finished there. Signing in takes the guardian password;
    signing out does not.
    """

    POLL_SECONDS = 3
    # As long as the daemon's `tailscale up` waits (tailnet.LOGIN_WAIT).
    POLL_FOR = 15 * 60

    def __init__(self, win):
        super().__init__(
            title="Tailscale",
            description=("Connects this computer to your tailnet, so the people who use "
                         "it can reach your other machines by name. Each account's filter "
                         "applies there as it does everywhere else."))
        self.win = win
        self.state: dict | None = None
        self.login_url: str | None = None
        self.busy: str | None = None
        self._rows: list[Gtk.Widget] = []
        self._poll_until = 0.0
        self.action = Gtk.Button(valign=Gtk.Align.CENTER, visible=False)
        self.action.connect("clicked", self._on_action)
        self.set_header_suffix(self.action)
        # Filled by refresh(), which the page calls on every reload.
        self._show()

    # -- reading ------------------------------------------------------------

    def refresh(self) -> None:
        if self.login_url or self.busy:
            return  # a sign-in is in progress; the poll owns the group
        self._show()
        run_async(self.win.client.tailscale_status, self._loaded,
                  lambda e: self._loaded({"error": error_text(e)}))

    def _loaded(self, state: dict) -> None:
        self.state = state
        self._show()

    # -- drawing ------------------------------------------------------------

    def _add(self, title: str, subtitle: str = "", *, spinner: bool = False,
             value: bool = False) -> Adw.ActionRow:
        row = Adw.ActionRow(title=title, subtitle=subtitle, subtitle_lines=4,
                            use_markup=False)
        if value:
            # A label and its value, with the value the line that is read.
            row.add_css_class("property")
            row.set_subtitle_selectable(True)
        if spinner:
            row.add_suffix(Gtk.Spinner(spinning=True, valign=Gtk.Align.CENTER))
        self.add(row)
        self._rows.append(row)
        return row

    def _button(self, label: str, style: str | None) -> None:
        self.action.set_label(label)
        for c in ("suggested-action", "destructive-action"):
            self.action.remove_css_class(c)
        if style:
            self.action.add_css_class(style)
        self.action.set_visible(True)
        self.action.set_sensitive(True)

    def _show(self) -> None:
        for row in self._rows:
            self.remove(row)
        self._rows = []
        self.action.set_visible(False)
        state = self.state

        if self.busy:
            self._add(self.busy, spinner=True)
            return
        if self.login_url:
            self._show_login()
            return
        if state is None:
            self._add("Checking…", spinner=True)
            return
        if state.get("error") and "installed" not in state:
            self._add("Could not check Tailscale", state["error"])
            self._button("Check Again", None)
            return
        if not state.get("installed", True):
            self._add("Tailscale is not installed on this computer")
            return
        if state.get("state") == "Unavailable":
            self._add("Tailscale is not running",
                      "Restarting the computer usually fixes it."
                      + (f" ({state['error']})" if state.get("error") else ""))
            self._button("Check Again", None)
            return
        if not state.get("signed_in"):
            self._add("Not signed in",
                      "Sign in with the Tailscale account your tailnet belongs to.")
            self._button("Sign In…", "suggested-action")
            return

        self._add("Signed in as", state.get("user") or "—", value=True)
        self._add("Tailnet", state.get("tailnet") or "—", value=True)
        addresses = ", ".join(state.get("addresses") or []) or "No address yet"
        self._add("This computer", f"{state.get('name') or '—'}\n{addresses}", value=True)
        self._add("Connection", self._connection_text(state), value=True)
        self._button("Sign Out", "destructive-action")

    @staticmethod
    def _connection_text(state: dict) -> str:
        if state.get("connected"):
            return "Connected"
        return {
            "Stopped": "Not connected. Tailscale is switched off on this computer.",
            "Starting": "Connecting…",
            "NeedsMachineAuth": ("Waiting for the tailnet's administrator to approve this "
                                 "computer in the Tailscale admin console."),
        }.get(state.get("state"), f"Not connected ({state.get('state')})")

    def _show_login(self) -> None:
        url = self.login_url or ""
        row = self._add("Open the sign-in page",
                        "Open it on this computer or on your phone, and sign in with the "
                        f"account your tailnet belongs to.\n{url}")
        row.set_subtitle_selectable(True)
        copy = Gtk.Button(label="Copy", valign=Gtk.Align.CENTER)
        copy.connect("clicked", lambda _b: self._copy(url))
        row.add_suffix(copy)
        open_button = Gtk.Button(label="Open", valign=Gtk.Align.CENTER)
        open_button.add_css_class("suggested-action")
        open_button.connect("clicked", lambda _b: self._open(url))
        row.add_suffix(open_button)
        self._add("Waiting for you to sign in…", spinner=True)
        pointer_cursors(self)

    def _copy(self, url: str) -> None:
        from gi.repository import Gdk

        self.get_clipboard().set_content(Gdk.ContentProvider.new_for_value(url))
        self.win.toast("Address copied")

    def _open(self, url: str) -> None:
        try:
            Gtk.UriLauncher.new(url).launch(self.get_root(), None, None, None)
        except Exception as e:  # noqa: BLE001 - say so; the address is on screen to copy
            self.win.toast(f"Could not open a browser: {error_text(e)}")

    # -- acting -------------------------------------------------------------

    def _on_action(self, _button) -> None:
        state = self.state or {}
        if state.get("error") or state.get("state") == "Unavailable":
            self.state = None
            self.refresh()
        elif state.get("signed_in"):
            confirm(self.win, "Sign out of Tailscale?",
                    "This computer will leave the tailnet. Nobody using it can reach "
                    "your other machines until it is signed in again.",
                    "Sign Out", self._sign_out)
        else:
            self.win.with_guardian(self._sign_in)

    def _sign_in(self, pw: str) -> None:
        self.busy = "Asking Tailscale for a sign-in page…"
        self._show()

        def got(url: str) -> None:
            self.busy = None
            if not url:
                self.win.toast("This computer is back on your tailnet")
                self.refresh()
                return
            self.login_url = url
            self._poll_until = time.monotonic() + self.POLL_FOR
            self._show()
            GLib.timeout_add_seconds(self.POLL_SECONDS, self._poll)

        def failed(e) -> None:
            self.busy = None
            self.win.toast(error_text(e))
            self.refresh()

        run_async(lambda: self.win.client.tailscale_sign_in(pw), got, failed)

    def _poll(self) -> bool:
        if not self.login_url:
            return False
        if time.monotonic() > self._poll_until:
            self.login_url = None
            self.win.toast("The sign-in page has expired. Sign in again.")
            self.refresh()
            return False

        def checked(state: dict) -> None:
            if self.login_url and state.get("signed_in"):
                self.login_url = None
                self.state = state
                self.win.toast("This computer is on your tailnet")
                self._show()

        run_async(self.win.client.tailscale_status, checked, lambda _e: None)
        return True

    def _sign_out(self) -> None:
        self.busy = "Signing out…"
        self._show()

        def done(_r) -> None:
            self.busy = None
            self.state = None
            self.win.toast("Signed out of Tailscale")
            self.refresh()

        def failed(e) -> None:
            self.busy = None
            self.win.toast(error_text(e))
            self.refresh()

        run_async(self.win.client.tailscale_sign_out, done, failed)


class AppsPage(_Page):
    """Curates the approved-app list. Installing is the Store's job."""

    def __init__(self, win):
        super().__init__(win, "Apps", "apps")
        self.group: Adw.PreferencesGroup | None = None
        self.search_group: Adw.PreferencesGroup | None = None
        self.search_entry: Adw.EntryRow | None = None
        self.approved: list[dict] = []
        self.installed: set[str] = set()
        # With fifty-odd apps approved, the search that adds another one is
        # a long scroll below them. Focusing it scrolls it into view.
        add = Gtk.Button(label="Add Apps…", valign=Gtk.Align.CENTER)
        add.add_css_class("suggested-action")
        add.connect("clicked", lambda _b: self.search_entry
                    and self.search_entry.grab_focus())
        self.header.pack_end(add)
        self.refresh()

    def refresh(self) -> None:
        def load():
            return (self.win.client.list_catalog().get("apps", []),
                    set(self.win.client.list_installed()))

        def on_done(result):
            self.approved, self.installed = result
            self.win.catalog_count = len(self.approved)
            self._render_approved()
            if self.search_group is None:
                self._build_search()

        run_async(load, on_done, lambda e: self.win.toast(error_text(e)))

    def _render_approved(self) -> None:
        if self.group is not None:
            self.prefs.remove(self.group)
        group = Adw.PreferencesGroup(
            title=f"Approved apps ({len(self.approved)})",
            description=("Apps available to accounts set to Approved apps only. Approval also permits "
                "apps that would otherwise be excluded by their content rating."))
        if not self.approved:
            row = Adw.ActionRow(title="No apps approved yet",
                                subtitle="Search below to approve apps")
            row.set_sensitive(False)
            group.add(row)
        for app in self.approved:
            ref = app["ref"]
            row = Adw.ActionRow(title=app.get("name", ref), subtitle=ref, use_markup=False)
            if ref in self.installed:
                row.add_suffix(tag("Installed"))
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
        self.prefs.add(group)
        if self.search_group is not None:
            self.prefs.remove(self.search_group)
            self.prefs.add(self.search_group)

    def _build_search(self) -> None:
        group = Adw.PreferencesGroup(
            title="Add apps",
            description=("Search Flathub to approve an app. To give an account broader store access, "
                "use its Apps tab."))
        entry = Adw.EntryRow(title="Search all apps")
        entry.set_show_apply_button(True)
        self.search_entry = entry
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
                clear(self.results_box)
                for app in shown:
                    row = Adw.ActionRow(title=app["name"],
                                        subtitle=app.get("summary") or app["ref"],
                                        use_markup=False)
                    btn = Gtk.Button(label="Approve", valign=Gtk.Align.CENTER)
                    btn.add_css_class("suggested-action")
                    btn.connect("clicked", lambda _b, a=app: self.win.call(
                        lambda: self.win.client.approve_app(
                            a["ref"], a["name"], a.get("summary", "")),
                        refresh=False, done_msg=f"{a['name']} approved")
                        or GLib.timeout_add(400, lambda: (self.refresh(), False)[1]))
                    row.add_suffix(btn)
                    self.results_box.append(row)

            run_async(lambda: self.win.client.search_apps(query), on_done,
                      lambda e: self._show_results_message(error_text(e)))

        entry.connect("apply", do_search)
        entry.connect("entry-activated", do_search)
        self.search_group = group
        self.prefs.add(group)

    def _show_results_message(self, text: str) -> None:
        clear(self.results_box)
        row = Adw.ActionRow(title=text)
        row.set_sensitive(False)
        self.results_box.append(row)


class UpdatesPage(_Page):
    def __init__(self, win):
        super().__init__(win, "Updates", "updates")
        group = Adw.PreferencesGroup()
        pretty = "KosherOS"
        try:
            for line in Path("/etc/os-release").read_text().splitlines():
                if line.startswith("PRETTY_NAME="):
                    pretty = line.split("=", 1)[1].strip('"')
        except OSError:
            pass
        group.add(Adw.ActionRow(title="Operating system", subtitle=pretty))
        self.status_row = Adw.ActionRow(
            title="Updates",
            subtitle=win.update_state or "Not checked yet.")
        check = Gtk.Button(label="Check", valign=Gtk.Align.CENTER)
        check.connect("clicked", self._check)
        apply_btn = Gtk.Button(label="Download Update", valign=Gtk.Align.CENTER)
        apply_btn.add_css_class("suggested-action")
        apply_btn.connect("clicked", self._apply)
        # Shown once an update is staged — by this page or by the timer —
        # so the restart that finishes the job is one click away rather
        # than something to go and find elsewhere.
        self.restart_button = Gtk.Button(label="Restart Now", valign=Gtk.Align.CENTER,
                                         visible=False)
        self.restart_button.add_css_class("suggested-action")
        self.restart_button.connect("clicked", lambda _b: self._confirm_restart())
        self.status_row.add_suffix(check)
        self.status_row.add_suffix(apply_btn)
        self.status_row.add_suffix(self.restart_button)
        self.apply_button = apply_btn
        group.add(self.status_row)
        # The bar under the row: an update pulls gigabytes, and a button
        # that goes dead for ten minutes is a button that gets pressed
        # again. It listens to the daemon's own signals, so it also shows
        # an update somebody else (or the timer) started.
        self.progress = Gtk.ProgressBar(show_text=True, visible=False,
                                        margin_top=8, margin_start=12, margin_end=12)
        group.add(self.progress)
        self.prefs.add(group)
        self.auto_group = AutoUpdatesGroup(win)
        self.prefs.add(self.auto_group)
        self._subscription: int | None = None
        self.connect("shown", lambda _p: self._listen())
        self.connect("hidden", lambda _p: self._stop_listening())

        # Which stream of builds this computer follows. Two rows rather
        # than a drop-down: each one can then carry the sentence that says
        # what it means, and the choice is made by pressing the thing you
        # want instead of by picking a word out of a list.
        self.channel_group = Adw.PreferencesGroup(
            title="Update channel",
            description=CHANNEL_INTRO)
        self.channel_box = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.channel_box.add_css_class("boxed-list")
        self.channel_group.add(self.channel_box)
        self.prefs.add(self.channel_group)
        self.channels: dict = {}
        self._load_channels()

        # Going back. Shown with the version it would return to, never as
        # a bare button: a "Roll back" with no target is the control that
        # gets clicked by accident.
        back = Adw.PreferencesGroup(
            title="Restore a previous version",
            description=("Restore the previous system version at the next restart. Files and settings "
                "are kept."))
        # No "running now" row: the Operating system row above already
        # names the version, and two rows saying it read as two things.
        self.back_row = Adw.ActionRow(title="Go back to the previous version",
                                      subtitle="Checking…", subtitle_lines=3)
        self.back_button = Gtk.Button(label="Go back", valign=Gtk.Align.CENTER,
                                      sensitive=False)
        self.back_button.connect("clicked", lambda _b: self._confirm_rollback())
        self.back_row.add_suffix(self.back_button)
        back.add(self.back_row)
        self.prefs.add(back)
        self.rollback_target: dict | None = None
        self._load_deployment()

    def _load_channels(self) -> None:
        run_async(self.win.client.list_channels, self.show_channels,
                  lambda e: self.channel_group.set_description(
                      f"Could not read the update channels: {error_text(e)}"))

    def show_channels(self, info: dict) -> None:
        """Fill the channel rows from a ListChannels answer.

        Every field can be missing — an older daemon has no channels at
        all — so a shape this does not recognise leaves the group empty
        with its explanation, rather than half a list.
        """
        self.channels = info or {}
        clear(self.channel_box)
        entries = self.channels.get("channels") or []
        effective = self.channels.get("pending") or self.channels.get("current")
        for entry in entries:
            name = entry.get("name") or ""
            row = Adw.ActionRow(title=entry.get("title") or name,
                                subtitle=entry.get("description") or "",
                                subtitle_lines=4)
            on_it = bool(name) and name == effective
            # Every row carries the tick, invisible on the ones that are
            # not in use, so the two titles line up under each other
            # instead of one sitting an icon's width to the right.
            icon = Gtk.Image(icon_name="emblem-ok-symbolic",
                             opacity=1.0 if on_it else 0.0)
            icon.add_css_class("success")
            row.add_prefix(icon)
            if on_it:
                mark = Gtk.Label(label="At the next restart"
                                 if self.channels.get("pending") else "In use",
                                 valign=Gtk.Align.CENTER)
                mark.add_css_class("dim-label")
                row.add_suffix(mark)
            else:
                button = Gtk.Button(label="Use this", valign=Gtk.Align.CENTER)
                button.connect("clicked", lambda _b, e=entry: self._ask_switch(e))
                row.add_suffix(button)
            self.channel_box.append(row)
        self.channel_group.set_description(CHANNEL_INTRO + channel_note(self.channels))

    def _ask_switch(self, entry: dict) -> None:
        title = entry.get("title") or entry.get("name") or "this channel"
        # Not the row's own description again — you have just read it. What
        # this adds is what pressing Switch does, and the warning on the
        # one direction that carries a cost.
        caution = entry.get("caution")
        confirm(self.win, f"Switch this computer to {title}?",
                (f"{caution}\n\n" if caution else "")
                + "The download starts now and the new version is used from "
                "the next restart. Settings and files are kept.",
                "Switch", lambda: self.win.with_guardian(
                    lambda pw: self._switch(entry.get("name") or "", title, pw)),
                destructive=False)

    def _switch(self, name: str, title: str, guardian_password: str) -> None:
        """Start the switch. It is the same pull as an update and reports on
        the same signals, so the same progress bar shows it."""
        self._listen()
        self.apply_button.set_sensitive(False)
        self.status_row.set_subtitle(f"Downloading the {title} version…")
        self.progress.set_fraction(0)
        self.progress.set_text("Starting…")
        self.progress.set_visible(True)

        def on_error(e):
            self.apply_button.set_sensitive(True)
            self.progress.set_visible(False)
            self.status_row.set_subtitle(error_text(e))
            self.win.toast(error_text(e))

        run_async(lambda: self.win.client.set_channel(name, guardian_password),
                  lambda _r: None, on_error)

    def _load_deployment(self) -> None:
        def on_done(status):
            self.show_deployment(status)

        run_async(self.win.client.deployment_status, on_done,
                  lambda e: self.show_deployment({"error": error_text(e)}))

    def show_deployment(self, status: dict) -> None:
        """Fill the version rows from a DeploymentStatus answer.

        Every key can be missing or None — bootc's JSON has moved between
        releases and the daemon degrades rather than raises — so this reads
        defensively and says "unknown" instead of crashing.
        """
        status = status or {}
        if status.get("error"):
            self.back_row.set_subtitle(f"Could not read the versions: {status['error']}")
            self.back_button.set_sensitive(False)
            return
        staged = status.get("staged") or None
        if staged:
            # The timer, or a previous visit, already fetched an update.
            self.status_row.set_subtitle(
                "An update is ready: " + _deployment_words(staged)
                + ". Restart the computer to start using it.")
            self.win.update_state = "Update ready — restart to use it"
            self.restart_button.set_visible(True)
            self.apply_button.set_visible(False)
        target = status.get("rollback") or None
        self.rollback_target = target
        if status.get("rollback_queued"):
            self.back_row.set_subtitle("Already going back at the next restart.")
            self.back_button.set_sensitive(False)
        elif target:
            self.back_row.set_subtitle("Would return to " + _deployment_words(target) + ".")
            self.back_button.set_sensitive(True)
        else:
            self.back_row.set_subtitle("There is no previous version to go back to.")
            self.back_button.set_sensitive(False)

    def _confirm_restart(self):
        return confirm(self.win, "Restart now?",
                "The update starts when the computer restarts. Anyone who is "
                "signed in will be signed out, so make sure nothing is left "
                "unsaved.", "Restart",
                lambda: self.win.call(self.win.client.reboot, refresh=False,
                                      done_msg="Restarting…"),
                destructive=False)

    def _confirm_rollback(self) -> None:
        target = self.rollback_target or {}
        version = target.get("version") or "the previous version"
        confirm(self.win, f"Go back to {version}?",
                "This computer will start the previous version of KosherOS at "
                "its next restart. Settings and files are kept. Do this if an "
                "update broke something.", "Go back",
                # The older version brings its own filter with it, so going
                # back takes the guardian password like a filter change.
                lambda: self.win.with_guardian(lambda pw: self.win.call(
                    lambda: self.win.client.rollback(pw), refresh=False,
                    done_msg=f"Going back to {version} at the next restart",
                    on_done=self._load_deployment)),
                destructive=False)

    def _check(self, _b) -> None:
        self.status_row.set_subtitle("Checking…")

        def on_done(info):
            sentence, short = check_words(info, self._running_version())
            self.win.update_state = short
            self.status_row.set_subtitle(sentence)
            if info.get("available"):
                # Already on the disk: staged wants a restart, and the one
                # you went back from is reached from Go back, not from here.
                on_disk = info.get("on_disk")
                self.apply_button.set_visible(not on_disk)
                self.restart_button.set_visible(on_disk == "staged")
            self.win.sidebar.refresh() if hasattr(self.win, "sidebar") else None

        run_async(self.win.client.check_update, on_done,
                  lambda e: self.status_row.set_subtitle(error_text(e)))

    def _running_version(self) -> str | None:
        booted = (getattr(self.win, "deployment", None) or {}).get("booted") or {}
        return booted.get("version") or None

    # -- applying, with the daemon telling us how far it is ----------------------

    def _listen(self) -> None:
        if self._subscription is None:
            self._subscription = self.win.client.connect_update_signals(
                self.on_progress, self.on_finished)

    def _stop_listening(self) -> None:
        if self._subscription is not None:
            self.win.client.disconnect_signals(self._subscription)
            self._subscription = None

    def _apply(self, _b) -> None:
        self._listen()  # in case the page was built but never shown (tests)
        self.apply_button.set_sensitive(False)
        self.status_row.set_subtitle("Updating…")
        self.progress.set_fraction(0)
        self.progress.set_text("Starting…")
        self.progress.set_visible(True)

        def on_error(e):
            self.apply_button.set_sensitive(True)
            self.progress.set_visible(False)
            self.status_row.set_subtitle(error_text(e))
            self.win.toast(error_text(e))

        run_async(self.win.client.apply_update, lambda _r: None, on_error)

    def on_progress(self, percent: int, status: str) -> None:
        self.apply_button.set_sensitive(False)
        self.progress.set_visible(True)
        if percent < 0:
            self.progress.pulse()
            self.progress.set_text(status)
        else:
            self.progress.set_fraction(percent / 100)
            self.progress.set_text(f"{status} · {percent}%")
        self.status_row.set_subtitle("Updating…")

    def on_finished(self, ok: bool, error: str) -> None:
        self.apply_button.set_sensitive(True)
        self.progress.set_visible(False)
        if ok:
            self.win.update_state = "Update ready — restart to use it"
            self.status_row.set_subtitle(
                "Restart to apply the update. Save your work first.")
            self.restart_button.set_visible(True)
            self.apply_button.set_visible(False)
            self.win.toast("Update ready — restart to apply")
            self._load_deployment()
            self._load_channels()
        else:
            self.status_row.set_subtitle(error or "The update did not finish.")
            self.win.toast(error or "The update did not finish")


AUTO_INTRO = ("New versions download in the background and are used the next time "
              "the computer restarts. It never restarts on its own.")
AUTO_SCHEDULES = ("off", "daily", "weekly", "biweekly", "monthly")
AUTO_LABELS = {"off": "Off", "daily": "Daily", "weekly": "Weekly",
               "biweekly": "Every 2 weeks", "monthly": "Monthly"}


def auto_words(status: dict | None, now: float | None = None) -> str:
    """The line under the schedule: when it last checked, or that the last
    try did not work. A failed check is never shown as up to date."""
    status = status or {}
    if status.get("schedule") == "off":
        return "Only when you press Check above."
    now = time.time() if now is None else now
    ok, tried = status.get("last_success") or 0, status.get("last_attempt") or 0
    if status.get("last_error") and tried >= ok:
        return "The last check did not work. It tries again tomorrow."
    if not ok:
        return "Has not checked yet. The first check is within a day."
    days = int((now - ok) // 86400)
    when = ("today" if days < 1 else "yesterday" if days < 2
            else f"{days} days ago")
    return f"Last checked {when}."


class AutoUpdatesGroup(Adw.PreferencesGroup):
    """How often the computer fetches updates on its own (#19), and whether
    the people signed in are told once when one is ready."""

    def __init__(self, win):
        super().__init__(title="Automatic updates", description=AUTO_INTRO)
        self.win = win
        settings = win.policy.get("updates") or {}
        self.schedule = settings.get("auto", "weekly")
        if self.schedule not in AUTO_SCHEDULES:
            self.schedule = "weekly"
        self.notify = bool(settings.get("notify", True))
        self.status: dict | None = None

        self.schedule_row = Adw.ComboRow(
            title="Download updates",
            model=Gtk.StringList.new([AUTO_LABELS[s] for s in AUTO_SCHEDULES]),
            selected=AUTO_SCHEDULES.index(self.schedule), subtitle_lines=2)
        self.schedule_row.connect("notify::selected", self._on_schedule)
        self.add(self.schedule_row)
        self.notify_row = Adw.SwitchRow(
            title="Tell people when an update is ready",
            subtitle=("One notice for each update. A small mark stays in the panel "
                      "until the computer restarts either way."),
            subtitle_lines=3, active=self.notify)
        self.notify_row.connect("notify::active", self._on_notify)
        self.add(self.notify_row)
        self.schedule_row.set_subtitle("Checking…")
        run_async(self.win.client.auto_update_status, self._loaded,
                  lambda e: self.schedule_row.set_subtitle(
                      f"Could not read when it last checked: {error_text(e)}"))

    def _loaded(self, status: dict) -> None:
        self.status = status
        self.schedule_row.set_subtitle(auto_words({**status, "schedule": self.schedule}))

    def _on_schedule(self, row, _param) -> None:
        wanted = AUTO_SCHEDULES[row.get_selected()]
        if wanted != self.schedule:
            self._save(wanted, self.notify)

    def _on_notify(self, row, _param) -> None:
        wanted = row.get_active()
        if wanted != self.notify:
            self._save(self.schedule, wanted)

    def _save(self, schedule: str, notify: bool) -> None:
        before = (self.schedule, self.notify)
        self.schedule_row.set_sensitive(False)
        self.notify_row.set_sensitive(False)
        self.schedule_row.set_subtitle("Saving…")

        def done(_r) -> None:
            self.schedule, self.notify = schedule, notify
            self.win.policy["updates"] = {"auto": schedule, "notify": notify}
            self._settle()
            self.win.toast("Automatic updates are off" if schedule == "off" else
                           f"Updates download {AUTO_LABELS[schedule].lower()}")

        def failed(e) -> None:
            self.schedule, self.notify = before
            self._settle()
            self.win.toast(error_text(e))

        run_async(lambda: self.win.client.set_auto_updates(schedule, notify), done, failed)

    def _settle(self) -> None:
        """Put the rows back to what is saved, without saving again."""
        self.schedule_row.handler_block_by_func(self._on_schedule)
        self.notify_row.handler_block_by_func(self._on_notify)
        self.schedule_row.set_selected(AUTO_SCHEDULES.index(self.schedule))
        self.notify_row.set_active(self.notify)
        self.schedule_row.handler_unblock_by_func(self._on_schedule)
        self.notify_row.handler_unblock_by_func(self._on_notify)
        self.schedule_row.set_sensitive(True)
        self.notify_row.set_sensitive(True)
        self.schedule_row.set_subtitle(auto_words({**(self.status or {}),
                                                   "schedule": self.schedule}))


CHANNEL_INTRO = "Stable is recommended for everyday use."


def channel_note(info: dict) -> str:
    """The sentence added to the group's explanation when the plain two-row
    list would not tell the whole truth: a switch already waiting for the
    next restart, or a computer that is on no channel at all."""
    pending = (info or {}).get("pending")
    if pending:
        titles = {c.get("name"): c.get("title") or c.get("name")
                  for c in (info.get("channels") or [])}
        return (f" This computer switches to {titles.get(pending, pending)} "
                "at the next restart.")
    if info and not info.get("current"):
        return (" This computer is on neither one. It is fixed at one version "
                "and will not update on its own; pick one to start getting "
                "updates again.")
    return ""


def check_words(info: dict, running: str | None) -> tuple[str, str]:
    """(the sentence for the row, the few words for the sidebar) from a
    CheckUpdate answer. The version is the point: 'what would I get' is the
    question, and bootc's first line names the image instead."""
    version = info.get("version")
    channel = info.get("channel")
    if info.get("available"):
        where = f" on the {channel} channel" if channel else ""
        if version and info.get("on_disk") == "staged":
            return (f"Version {version} is downloaded. Restart to start using it.",
                    "Update ready")
        if version and info.get("on_disk") == "rollback":
            return (f"Version {version} is the newest{where}, and it is already on this "
                    f"computer: it is the one you went back from. Going back again "
                    f"returns to it; the next version will download here.",
                    f"{version} is the one you left")
        if version:
            sentence = f"Version {version} is available"
            if running:
                sentence += f" — you are on {running}"
            if channel:
                sentence += f" ({channel} channel)"
            return sentence + ".", f"{version} available"
        sentence = "An update is available"
        if channel:
            sentence += f" on the {channel} channel"
        return sentence + ".", "Update available"
    if info.get("available") is False:
        if running:
            return f"Up to date — {running} is the newest on the {channel or 'update'} channel.", "Up to date"
        return "Up to date.", "Up to date"
    if info.get("ok") is False:
        # The check never reached the update server, so nothing is known
        # about newer versions; saying "up to date" here was wrong (#207).
        return ("Couldn't check for updates. Make sure the computer is connected "
                "to the internet, then check again.", "Couldn't check")
    raw = (info.get("raw") or "").strip()
    first = next((line for line in raw.splitlines() if line.strip()), "Could not check.")
    return first[:120], first[:24]


def _channel_of(image: str | None) -> str | None:
    """'ghcr.io/x/kosher-linux:edge' -> 'edge'; a digest reference has none."""
    if not image or "@" in image:
        return None
    tail = image.rsplit("/", 1)[-1]
    return tail.split(":", 1)[1] if ":" in tail else None


def _deployment_words(entry: dict | None) -> str:
    """'version 0.1.0-pre.064 (edge channel)' from one deployment record —
    the version is what a person matches to a release; the channel is a
    word, not the whole image reference — or 'unknown' when bootc did not
    say."""
    if not entry:
        return "unknown"
    version = entry.get("version")
    image = entry.get("image")
    channel = entry.get("channel") or _channel_of(image)
    if version:
        return f"version {version}" + (f" ({channel} channel)" if channel else "")
    return image or "unknown"
