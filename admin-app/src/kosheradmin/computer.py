"""This computer: the settings that are not about one person.

The sidebar's second section. Three pages, each a real page rather than a
tile with two words on it:

  Protection — is the filter actually working, what it blocks for
               everyone, and the guardian password that guards the answer
  Apps       — which apps this computer may install at all
  Updates    — what version it runs, and the way back if one goes wrong

"Applies to everyone" is the honest heading for the middle group. Ad
blocking is rendered into the machine's own resolvers, the word lists are
one list per machine, and neither can be answered differently per account
— so they belong here, with the reason written next to them, rather than
on a person's page where they would look per-person.
"""

from __future__ import annotations

from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

from . import labels  # noqa: E402
from .common import clear, confirm, error_text, pointer_cursors, run_async, tag  # noqa: E402
from .dialogs import ListEditDialog, guardian_dialog  # noqa: E402


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
                 "kosherd did not answer. Try again in a moment.", False)]
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


def health_summary(status: dict | None) -> tuple[str, bool]:
    """Two words for the sidebar: what the health is, and whether it is fine."""
    rows = health_rows(status)
    problems = [r for r in rows if not r[2]]
    if not problems:
        return "Running", True
    if len(problems) == 1:
        return "1 problem", False
    return f"{len(problems)} problems", False


class _Page(Adw.NavigationPage):
    """A page with a header bar and a preferences page inside it."""

    def __init__(self, win, title: str, tag: str):
        super().__init__(title=title, tag=tag)
        self.win = win
        self.prefs = Adw.PreferencesPage()
        self.header = Adw.HeaderBar()
        view = Adw.ToolbarView()
        view.add_top_bar(self.header)
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
        self.health_group = Adw.PreferencesGroup(title="Is the filter working?")
        check = Gtk.Button(label="Check again", valign=Gtk.Align.CENTER)
        check.connect("clicked", lambda _b: self._check())
        self.health_group.set_header_suffix(check)
        self.health_rows_box = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.health_rows_box.add_css_class("boxed-list")
        self.health_group.add(self.health_rows_box)
        self.prefs.add(self.health_group)

        everyone = Adw.PreferencesGroup(
            title="Applies to everyone",
            description="One answer for the whole computer: every account, "
                        "the guest included, and every browser and app. "
                        "These cannot be set per person.")
        self.ads_row = Adw.SwitchRow(
            title="Block ads and trackers",
            subtitle="At this computer's own resolver, like a Pi-hole, so it "
                     "covers every account and every app rather than one "
                     "browser. An ad network is also where immodest pictures "
                     "arrive uninvited, so on is the right answer for almost "
                     "everyone.",
            subtitle_lines=4,
            active=win.policy.get("adblock", {}).get("enabled", True))
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
            title="Who may change all this",
            description="Every setting in KosherOS Admin already needs an "
                        "administrator. A guardian password adds a second one "
                        "— the other spouse's, say — that any weakening of "
                        "the filter also needs.")
        self.guardian_row = Adw.ActionRow(
            title="Guardian password", subtitle="—", activatable=True)
        self.guardian_row.add_suffix(Gtk.Image(icon_name="go-next-symbolic"))
        self.guardian_row.connect("activated", lambda _r: guardian_dialog(win))
        locking.add(self.guardian_row)
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

        on = self.win.policy.get("guardian", {}).get("enabled", False)
        self.guardian_row.set_subtitle(
            "On. Weakening the filter asks for it once per session."
            if on else "Off. Any administrator can weaken the filter alone.")

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
            description="Anyone using this computer may install these from the "
                        "KosherOS Store. Nothing else can be installed.")
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
            description="Search everything available, then approve what you "
                        "want people on this computer to be able to install.")
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
            subtitle=win.update_state or "Not checked yet. KosherOS also "
                                        "checks on its own in the background.")
        check = Gtk.Button(label="Check", valign=Gtk.Align.CENTER)
        check.connect("clicked", self._check)
        apply_btn = Gtk.Button(label="Update Now", valign=Gtk.Align.CENTER)
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
        self._subscription: int | None = None
        self.connect("shown", lambda _p: self._listen())
        self.connect("hidden", lambda _p: self._stop_listening())

        # Going back. Shown with the version it would return to, never as
        # a bare button: a "Roll back" with no target is the control that
        # gets clicked by accident.
        back = Adw.PreferencesGroup(
            title="If an update went wrong",
            description="The previous version stays on disk. Going back to it "
                        "takes effect at the next restart and keeps every "
                        "setting and file.")
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
                lambda: self.win.call(
                    self.win.client.rollback, refresh=False,
                    done_msg=f"Going back to {version} at the next restart",
                    on_done=self._load_deployment),
                destructive=False)

    def _check(self, _b) -> None:
        self.status_row.set_subtitle("Checking…")

        def on_done(info):
            sentence, short = check_words(info, self._running_version())
            self.win.update_state = short
            self.status_row.set_subtitle(sentence)
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
                "The update is ready. Restart the computer to start using it; "
                "until then everything carries on as it is.")
            self.restart_button.set_visible(True)
            self.apply_button.set_visible(False)
            self.win.toast("Update ready — restart to apply")
            self._load_deployment()
        else:
            self.status_row.set_subtitle(error or "The update did not finish.")
            self.win.toast(error or "The update did not finish")


def check_words(info: dict, running: str | None) -> tuple[str, str]:
    """(the sentence for the row, the few words for the sidebar) from a
    CheckUpdate answer. The version is the point: 'what would I get' is the
    question, and bootc's first line names the image instead."""
    version = info.get("version")
    channel = info.get("channel")
    if info.get("available"):
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
    raw = (info.get("raw") or "").strip()
    first = next((line for line in raw.splitlines() if line.strip()), "Could not check.")
    return first[:120], first[:24]


def _deployment_words(entry: dict | None) -> str:
    """'version 2026.09.08 (ghcr.io/…/kosheros:stable)' from one deployment
    record, or 'unknown' when bootc did not say."""
    if not entry:
        return "unknown"
    version = entry.get("version")
    image = entry.get("image")
    if version and image:
        return f"version {version} ({image})"
    return f"version {version}" if version else (image or "unknown")
