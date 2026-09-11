"""The family board: the home screen.

One card per person, each the same four lines in the same order (web,
pictures, video, apps) so the eye compares across children, with what
happened today at the bottom. Above it, two banners that appear only when
they have something to say: the requests waiting for an answer (blue, the
one thing anyone is blocked on) and the filter's health (amber, the one
thing that is dangerous). Below it, the computer-wide settings as tiles.
"""

from __future__ import annotations

import time
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

from kosherd import profiles as profiles_mod  # noqa: E402

from . import labels  # noqa: E402
from .common import avatar, clear, confirm, error_text, icon_line, run_async, tag  # noqa: E402
from .dialogs import (HealthDialog, ListEditDialog, RequestsDialog,  # noqa: E402
                      WhitelistDialog, guardian_dialog)


def health_rows(status: dict | None) -> list[tuple[str, str, bool]]:
    """(title, body, ok) for everything the filter status has to say.

    A filter that has quietly stopped doing something is worse than one
    that never did it, because the family is relying on it — and the
    version of this that says nothing teaches people that blank pictures
    mean the computer is broken. So there is always at least one row, and
    when everything is fine it says so, with the time it was checked.
    """
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
                     "Until it is, what it enforces is not being enforced.", False))
    for problem in status.get("problems", []):
        rows.append(("A filter list is missing or damaged",
                     problem[0].upper() + problem[1:] + ".", False))
    running = [s for s, state in (status.get("services") or {}).items()
               if state == "active"]
    checked = time.strftime("%H:%M")
    if rows:
        if running:
            rows.append(("Everything else is running",
                         f"{len(running)} of {len(status.get('services') or {})} "
                         f"filter services are up. Checked at {checked}.", True))
    else:
        rows.append(("The filter is running",
                     "Web filter, DNS filter and search are all up, and every "
                     f"list loaded. Checked at {checked}.", True))
    return rows


class FamilyPage(Gtk.Box):
    def __init__(self, win):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.win = win

        # The user asked for "a blue notification at the top: X requests
        # waiting for you, click to deal with" — not a list group.
        self.requests_banner = Adw.Banner(button_label="Deal with them")
        self.requests_banner.add_css_class("requests-banner")
        self.requests_banner.connect("button-clicked", lambda _b: self.open_requests())
        self.append(self.requests_banner)

        self.health_banner = Adw.Banner(button_label="Details")
        self.health_banner.add_css_class("health-banner")
        self.health_banner.connect("button-clicked", lambda _b: self.open_health())
        self.append(self.health_banner)

        # Not an Adw.PreferencesPage: its clamp is 600px, which is one card
        # wide. The board wants three across, so it gets its own clamp.
        self.body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=28,
                            margin_top=22, margin_bottom=28, margin_start=18,
                            margin_end=18)
        clamp = Adw.Clamp(maximum_size=1080, tightening_threshold=900)
        clamp.set_child(self.body)
        self.scroller = Gtk.ScrolledWindow(vexpand=True,
                                           hscrollbar_policy=Gtk.PolicyType.NEVER)
        self.scroller.set_child(clamp)
        self.append(self.scroller)

        self.family_group = Adw.PreferencesGroup(title="The family")
        self.health_tag = tag("", "ok")
        self.family_group.set_header_suffix(self.health_tag)
        self.cards = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE,
                                 min_children_per_line=1, max_children_per_line=3,
                                 column_spacing=12, row_spacing=12, homogeneous=True,
                                 activate_on_single_click=True)
        self.cards.connect("child-activated", self._on_card)
        self.family_group.add(self.cards)
        self.body.append(self.family_group)

        self.computer_group = Adw.PreferencesGroup(
            title="This computer",
            description="Settings that apply to every account.")
        self.tiles = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE,
                                 min_children_per_line=2, max_children_per_line=5,
                                 column_spacing=10, row_spacing=10, homogeneous=True,
                                 activate_on_single_click=True)
        self.tiles.connect("child-activated", self._on_tile)
        self.computer_group.add(self.tiles)
        self.body.append(self.computer_group)

    def scroller_to_end(self) -> None:
        adjustment = self.scroller.get_vadjustment()
        adjustment.set_value(adjustment.get_upper())

    # -- data -> widgets ------------------------------------------------------------

    def refresh(self) -> None:
        win = self.win
        waiting = win.requests
        n = len(waiting)
        if n:
            self.requests_banner.set_title(
                f"{labels.plural(n, 'request')} waiting for you")
            self.requests_banner.set_revealed(True)
        else:
            self.requests_banner.set_revealed(False)

        rows = health_rows(win.status)
        problems = [r for r in rows if not r[2]]
        if problems:
            self.health_banner.set_title(problems[0][0])
            self.health_banner.set_revealed(True)
            self.health_tag.set_visible(False)
        else:
            self.health_banner.set_revealed(False)
            self.health_tag.set_label(f"Filter running · checked {time.strftime('%H:%M')}")
            self.health_tag.set_visible(True)

        clear(self.cards)
        by_uid: dict[int, int] = {}
        for request in waiting:
            by_uid[request["uid"]] = by_uid.get(request["uid"], 0) + 1
        custom = win.policy.get("custom_profiles", [])
        for user in win.policy.get("users", []):
            counts = (win.summary or {}).get(str(user["uid"]))
            card = person_card(user, counts, by_uid.get(user["uid"], 0), custom)
            child = Gtk.FlowBoxChild()
            child.set_child(card)
            child.user = user
            self.cards.append(child)
        guest = Gtk.FlowBoxChild()
        guest.set_child(guest_card(win.policy.get("guest") or {"enabled": False}, custom))
        guest.user = None
        self.cards.append(guest)

        clear(self.tiles)
        for key, title, value in self._tile_values():
            child = Gtk.FlowBoxChild()
            child.set_child(tile(title, value))
            child.key = key
            self.tiles.append(child)

    def _tile_values(self) -> list[tuple[str, str, str]]:
        win = self.win
        approved = win.catalog_count
        apps = (f"{labels.plural(approved, 'app')} approved" if approved is not None
                else "Approved apps")
        adblock = win.policy.get("adblock", {}).get("enabled", True)
        guardian = win.policy.get("guardian", {}).get("enabled", False)
        return [
            ("apps", "Apps", apps),
            ("lists", "Word lists", "Bad language, blocked searches"),
            ("ads", "Ads and trackers", "Blocked for everyone" if adblock else "Off"),
            ("guardian", "Guardian password", "On" if guardian else "Off"),
            ("updates", "Updates", win.update_state or "Check for updates"),
        ]

    # -- actions ------------------------------------------------------------------------

    def _on_card(self, _box, child) -> None:
        user = getattr(child, "user", None)
        if user is None:
            self.win.push(GuestPage(self.win))
        else:
            self.win.open_user_detail(user)

    def _on_tile(self, _box, child) -> None:
        key = getattr(child, "key", "")
        if key == "apps":
            self.win.push(AppsPage(self.win))
        elif key == "lists":
            self.win.push(WordListsPage(self.win))
        elif key == "ads":
            self.win.push(AdsPage(self.win))
        elif key == "guardian":
            guardian_dialog(self.win)
        elif key == "updates":
            self.win.push(UpdatesPage(self.win))

    def open_requests(self) -> RequestsDialog:
        dialog = RequestsDialog(self.win, self.win.requests)
        dialog.present(self.win)
        return dialog

    def open_health(self) -> HealthDialog:
        dialog = HealthDialog(self.win, health_rows(self.win.status))
        dialog.present(self.win)
        return dialog


# -- cards ---------------------------------------------------------------------------------

def person_card(user: dict, counts: dict | None, waiting: int, custom=()) -> Gtk.Box:
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, width_request=240)
    box.add_css_class("card")
    box.add_css_class("person-card")

    who = Gtk.Box(spacing=10)
    who.append(avatar(user["username"], 36))
    names = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
    name = Gtk.Label(label=user["username"], xalign=0, ellipsize=3)
    name.add_css_class("heading")
    names.append(name)
    key, changes = profiles_mod.diff(user, custom)
    setup = labels.setup_sentence(key, changes, custom)
    if user.get("admin"):
        setup = "Admin · " + setup
    sub = Gtk.Label(label=setup, xalign=0, ellipsize=3)
    sub.add_css_class("dim-label")
    sub.add_css_class("caption")
    names.append(sub)
    who.append(names)
    box.append(who)

    lines = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
    for icon, text in labels.protection_lines(user):
        lines.append(icon_line(icon, text))
    box.append(lines)

    foot = Gtk.Box(spacing=6, margin_top=2)
    foot.append(Gtk.Separator(hexpand=True, visible=False))
    today = Gtk.Label(label=labels.today_sentence(counts), xalign=0, hexpand=True,
                      ellipsize=3)
    today.add_css_class("dim-label")
    today.add_css_class("caption")
    foot.append(today)
    if waiting:
        foot.append(tag(labels.plural(waiting, "request"), "acc"))
    box.append(Gtk.Separator())
    box.append(foot)
    return box


def guest_card(guest: dict, custom=()) -> Gtk.Box:
    # The same size as every other card: the box fills its cell and the
    # content is centred inside it, instead of the box shrinking to fit.
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8,
                  valign=Gtk.Align.FILL, halign=Gtk.Align.FILL, width_request=240)
    box.add_css_class("card")
    box.add_css_class("person-card")
    enabled = guest.get("enabled", False)
    if not enabled:
        box.add_css_class("guest-off")
    box.append(Gtk.Box(vexpand=True))
    box.append(avatar("Guest", 36))
    name = Gtk.Label(label="Guest", halign=Gtk.Align.CENTER)
    name.add_css_class("heading")
    box.append(name)
    if enabled:
        key = profiles_mod.matching(guest, custom)
        setup = profiles_mod.get(key, custom).label if key else \
            labels.MODE_LABELS.get(guest.get("mode", "whitelist"), "")
        text = f"On · {setup}. Wiped at sign-out."
    else:
        text = "Off. Passwordless, wiped at sign-out."
    sub = Gtk.Label(label=text, halign=Gtk.Align.CENTER, wrap=True, justify=2)
    sub.add_css_class("dim-label")
    sub.add_css_class("caption")
    box.append(sub)
    hint = Gtk.Label(label="Set up" if enabled else "Turn on", halign=Gtk.Align.CENTER)
    hint.add_css_class("accent")
    hint.add_css_class("caption")
    box.append(hint)
    box.append(Gtk.Box(vexpand=True))
    return box


def tile(title: str, value: str) -> Gtk.Box:
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
    box.add_css_class("card")
    box.add_css_class("tile")
    name = Gtk.Label(label=title, xalign=0, ellipsize=3)
    name.add_css_class("heading")
    box.append(name)
    sub = Gtk.Label(label=value, xalign=0, ellipsize=3)
    sub.add_css_class("dim-label")
    sub.add_css_class("caption")
    box.append(sub)
    return box


# -- the pages behind the tiles -------------------------------------------------------------

class _Page(Adw.NavigationPage):
    """A pushed page with a header bar and a preferences page."""

    def __init__(self, win, title: str, tag: str):
        super().__init__(title=title, tag=tag)
        self.win = win
        self.prefs = Adw.PreferencesPage()
        view = Adw.ToolbarView()
        view.add_top_bar(Adw.HeaderBar())
        view.set_content(self.prefs)
        self.set_child(view)


class WordListsPage(_Page):
    """The three lists a family may add to. Machine-wide, not per account:
    a word is either bad language in this house or it is not."""

    def __init__(self, win):
        super().__init__(win, "Word lists", "lists")
        group = Adw.PreferencesGroup(
            description="These come complete. A family adds or removes a "
                        "handful at most; the shipped lists cover the rest.")
        for name, title, description, noun in labels.EDITABLE_LISTS:
            row = Adw.ActionRow(title=title, subtitle=description,
                                subtitle_lines=3, activatable=True)
            row.add_suffix(Gtk.Image(icon_name="go-next-symbolic"))
            row.connect("activated", lambda _r, n=name, t=title, d=description,
                        u=noun: ListEditDialog(win, n, t, d, u).present(win))
            group.add(row)
        self.prefs.add(group)


class AdsPage(_Page):
    def __init__(self, win):
        super().__init__(win, "Ads and trackers", "ads")
        group = Adw.PreferencesGroup()
        # Machine-wide, like a Pi-hole: every account, every browser, every
        # app, because all DNS on this machine goes through its resolvers.
        self.row = Adw.SwitchRow(
            title="Block ads and trackers everywhere",
            subtitle="At this computer's own resolver, so it covers every "
                     "account and every app, not one browser. On is the "
                     "right answer for almost everyone.",
            subtitle_lines=3,
            active=win.policy.get("adblock", {}).get("enabled", True))
        self.row.connect("notify::active", self._on_toggle)
        group.add(self.row)
        self.prefs.add(group)

    def _on_toggle(self, switch, _param) -> None:
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
        self.status_row = Adw.ActionRow(title="Updates",
                                        subtitle=win.update_state or "—")
        check = Gtk.Button(label="Check", valign=Gtk.Align.CENTER)
        check.connect("clicked", self._check)
        apply_btn = Gtk.Button(label="Update Now", valign=Gtk.Align.CENTER)
        apply_btn.add_css_class("suggested-action")
        apply_btn.connect("clicked", self._apply)
        self.status_row.add_suffix(check)
        self.status_row.add_suffix(apply_btn)
        group.add(self.status_row)
        self.prefs.add(group)

        # Going back. Shown with the version it would return to, never as
        # a bare button: a "Roll back" with no target is the control that
        # gets clicked by accident.
        back = Adw.PreferencesGroup(
            title="If an update went wrong",
            description="The previous version stays on disk. Going back to it "
                        "takes effect at the next restart and keeps every "
                        "setting and file.")
        self.version_row = Adw.ActionRow(title="Running now", subtitle="Checking…")
        back.add(self.version_row)
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
            self.version_row.set_subtitle(f"Could not read: {status['error']}")
        else:
            self.version_row.set_subtitle(_deployment_words(status.get("booted")))
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

        def on_done(status):
            first = next((line for line in status.splitlines() if line.strip()),
                         "Up to date")
            self.win.update_state = first[:120]
            self.status_row.set_subtitle(first[:120])

        run_async(self.win.client.check_update, on_done,
                  lambda e: self.status_row.set_subtitle(error_text(e)))

    def _apply(self, _b) -> None:
        self.status_row.set_subtitle("Updating (staged on reboot when done)…")
        self.win.call(self.win.client.apply_update, refresh=False,
                      done_msg="Update staged — reboot to apply")


# The guest's kinds of internet, each with the preset whose content settings
# go with it. Filtered means the Child preset: a guest is a stranger to the
# filter, and the strict answer is the one that is right without knowing them.
GUEST_KINDS = {
    "none": "none",
    "whitelist": "young_child",
    "dnsfilter": "dns_only",
    "filtered": "child",
    "unfiltered": "unfiltered",
}
GUEST_KIND_HINTS = {
    "none": "No web at all for whoever signs in as the guest.",
    "whitelist": "Only the approved sites below, no pictures from the web, no YouTube.",
    "dnsfilter": "Known bad sites blocked and safe search forced; pages and pictures not checked.",
    "filtered": "Filtered like a child's account: adult, gambling, social and video "
                "blocked, immodest pictures hidden, bad language replaced.",
    "unfiltered": "Nothing filtered for the guest.",
}


class GuestPage(_Page):
    """The guest is an account like the others: a preset, and for the
    whitelist preset, its own list. Passwordless, and wiped at sign-out."""

    def __init__(self, win):
        super().__init__(win, "Guest", "guest")
        guest = win.policy.get("guest", {"enabled": False})
        group = Adw.PreferencesGroup(
            title="Guest account",
            description="Passwordless account; all guest data is erased at sign-out.")

        mode = guest.get("mode", "whitelist")
        wl_domains = guest.get("whitelist", [])

        def push(enabled, new_mode, domains):
            win.with_guardian(lambda pw: win.call(
                lambda: win.client.set_guest_config(enabled, new_mode, domains, pw),
                done_msg="Guest settings saved"))

        switch = Adw.SwitchRow(title="Enable guest account", active=guest["enabled"])
        switch.connect("notify::active",
                       lambda s, _p: s.get_active() != guest["enabled"] and
                       push(s.get_active(), mode, wl_domains))
        group.add(switch)

        # The guest is set up by the KIND of internet it gets, not by a
        # preset: nobody knows who the guest is, so "Child" or "Teenager"
        # is the wrong question. Each kind maps to the preset that carries
        # sensible content settings for it (GUEST_KINDS), so a filtered
        # guest still gets pictures, language and YouTube handled.
        kinds = list(GUEST_KINDS)
        mode_row = Adw.ComboRow(
            title="Internet for the guest",
            model=Gtk.StringList.new([labels.MODE_LABELS[m] for m in kinds]))
        mode_row.set_selected(kinds.index(mode) if mode in kinds else 0)
        mode_row.set_subtitle(GUEST_KIND_HINTS.get(mode, labels.MODE_HINTS.get(mode, "")))
        mode_row.set_subtitle_lines(3)

        def on_guest_kind(combo, _p):
            new_mode = kinds[combo.get_selected()]
            if new_mode == mode:
                return
            push(guest["enabled"], GUEST_KINDS[new_mode], wl_domains)

        mode_row.connect("notify::selected", on_guest_kind)
        group.add(mode_row)

        wl_row = Adw.ActionRow(
            title="Approved sites",
            subtitle=labels.plural(len(wl_domains), "domain"), activatable=True)
        wl_row.add_suffix(Gtk.Image(icon_name="go-next-symbolic"))
        wl_row.connect("activated", lambda _r: WhitelistDialog(
            win, "Approved sites — Guest", wl_domains,
            lambda new: push(guest["enabled"], mode, new)).present(win))
        if mode != "whitelist":
            wl_row.set_sensitive(False)
            wl_row.set_subtitle("Only used in Whitelist only mode")
        group.add(wl_row)
        self.prefs.add(group)


class AppsPage(_Page):
    """Curates the approved-app list. Installing is the Store's job."""

    def __init__(self, win):
        super().__init__(win, "Apps", "apps")
        self.group: Adw.PreferencesGroup | None = None
        self.search_group: Adw.PreferencesGroup | None = None
        self.approved: list[dict] = []
        self.installed: set[str] = set()
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

