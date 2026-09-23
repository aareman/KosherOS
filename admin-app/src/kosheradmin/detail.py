"""One person, one full page.

The top answers "how is Yosef protected?" before any setting is touched:
the group and how far the account has drifted from it, and the protection
as a strip of chips. Then six tabs with room to breathe. The first tab is
what happened, not a form — what was blocked today, with Allow on each
row, and who changed what.
"""

from __future__ import annotations

import time

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

from kosherd import activity as activity_mod  # noqa: E402
from kosherd import appaccess, appkinds  # noqa: E402
from kosherd import profiles as profiles_mod  # noqa: E402
from kosherd import timelimits  # noqa: E402
from kosherd.policy import MEDIA_LEVELS, MODES, YOUTUBE_CATEGORIES  # noqa: E402

from . import labels  # noqa: E402
from .common import (banner_slot, avatar, chip, clear, confirm, error_text, mode_badge,  # noqa: E402
                     pointer_cursors, run_async, small_button, tag)
from .dialogs import (ApproveChannelDialog, RulesDialog, SavePresetDialog,  # noqa: E402
                      WhitelistDialog, allow_menu, confirm_remove_user, request_row)
from .feed import fold  # noqa: E402
from .schedule import ScheduleGrid, legend  # noqa: E402

TABS = (("overview", "Overview", "view-list-symbolic"),
        ("filtering", "Filtering", "web-browser-symbolic"),
        # "Pictures & words" no longer fits the switcher beside the
        # sidebar, and a truncated tab is worse than a shorter one.
        ("media", "Pictures", "image-x-generic-symbolic"),
        ("youtube", "YouTube", "video-display-symbolic"),
        ("time", "Time", "alarm-symbolic"),
        ("apps", "Apps", "view-grid-symbolic"),
        ("account", "Account", "system-users-symbolic"))


def hint_under(group: Adw.PreferencesGroup, text: str) -> Gtk.Label:
    """A caption beneath a group's rows.

    Combo rows with long subtitles squeeze their selected value into
    "Filtered int…", because the subtitle takes the width first. So the
    explanation lives under the list instead, and updates with the choice.
    """
    label = Gtk.Label(label=text, xalign=0, wrap=True, margin_top=6, margin_start=6,
                      margin_end=6)
    label.add_css_class("dim-label")
    label.add_css_class("caption")
    group.add(label)
    return label


def default_blocks() -> set[str]:
    """Categories every filtering account blocks unless somebody unblocks
    them. Labelled on the grid so a parent knows those are not the ones to
    think about."""
    from kosherd.categories import DEFAULT_BLOCKED

    return set(DEFAULT_BLOCKED)


class UserDetailPage(Adw.NavigationPage):
    def __init__(self, win, user: dict):
        super().__init__(title=user["username"], tag=f"user-{user['uid']}")
        self.win = win
        self.uid = user["uid"]
        self.user = user
        self._build(user)

    def rebuild(self, user: dict) -> None:
        selected = self.stack.get_visible_child_name()
        self._build(user)
        if selected:
            self.stack.set_visible_child_name(selected)

    # -- frame --------------------------------------------------------------------

    def _build(self, user: dict) -> None:
        self.user = user
        custom = self.win.policy.get("custom_profiles", [])
        self.preset_key, self.drift = profiles_mod.diff(user, custom)
        self.stack = Adw.ViewStack()
        builders = {"overview": self._overview_tab, "filtering": self._filtering_tab,
                    "media": self._media_tab, "youtube": self._youtube_tab,
                    "time": self._time_tab,
                    "apps": self._apps_tab, "account": self._account_tab}
        tabs = TABS
        if user.get("guest"):
            # A guest installs nothing and cannot be an admin or be removed;
            # its Account tab is the switch that turns it off.
            builders["account"] = self._guest_account_tab
            tabs = tuple(t for t in TABS if t[0] != "apps")
        for name, title, icon in tabs:
            page = Adw.PreferencesPage()
            for group in builders[name](user):
                page.add(group)
            self.stack.add_titled_with_icon(page, name, title, icon)

        # Just the name up here. The line that used to sit under it ("Child,
        # with 2 changes · Filtered internet") repeated what the page says
        # directly below, in a space too narrow for it, and crowded the tabs.
        header = Adw.HeaderBar(title_widget=Adw.WindowTitle(title=user["username"],
                                                            subtitle=""))
        # The page's own actions go where GNOME puts them: the header bar,
        # not a row of buttons beside the name. pack_end packs right to
        # left, so Save (the primary) is packed first and sits rightmost.
        for button in reversed(self._header_actions(user)):
            header.pack_end(button)
        self.banner_slot = banner_slot()
        self.switcher = Adw.ViewSwitcher(stack=self.stack,
                                         policy=Adw.ViewSwitcherPolicy.WIDE,
                                         halign=Gtk.Align.CENTER, margin_top=6,
                                         margin_bottom=6)
        self.switcher_bar = Adw.ViewSwitcherBar(stack=self.stack)
        view = Adw.ToolbarView()
        view.add_top_bar(header)
        view.add_top_bar(self.banner_slot)
        view.add_top_bar(self.switcher)
        view.add_bottom_bar(self.switcher_bar)
        view.set_content(self.stack)
        # Under 820sp the tabs go to the bottom bar, icons over short labels,
        # where seven of them fit on a phone-width pane.
        holder = Adw.BreakpointBin(width_request=320, height_request=240)
        holder.set_child(view)
        narrow = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 820sp"))
        narrow.add_setter(self.switcher, "visible", False)
        narrow.add_setter(self.switcher_bar, "reveal", True)
        holder.add_breakpoint(narrow)
        self.set_child(holder)
        pointer_cursors(self)
        # A rebuild replaced the slot the window's banner was sitting in,
        # and the old slot took the banner down with it: ask for it back.
        win = self.win
        nav = getattr(win, "nav", None)
        if nav is not None and nav.get_visible_page() is self:
            win.place_banner(self)

    def _gated(self, work, done_msg: str) -> None:
        self.win.with_guardian(lambda pw: self.win.call(lambda: work(pw), done_msg=done_msg))

    # -- overview ------------------------------------------------------------------

    def _overview_tab(self, user: dict) -> list[Adw.PreferencesGroup]:
        return [self._identity_group(user), *self._today_groups(user)]

    def _header_actions(self, user: dict) -> list[Gtk.Button]:
        """The page's actions, left to right. When the account has drifted
        from its group there are two ways to close the gap: put the account
        back, or give the whole group what was changed here — "update one
        and apply to all". Save as a group is always there."""
        custom = self.win.policy.get("custom_profiles", [])
        buttons = []
        if self.preset_key and labels.drift_sentences(self.drift):
            group = profiles_mod.get(self.preset_key, custom)
            reset = small_button(f"Reset to {group.label}")
            reset.connect("clicked", lambda _b: self._gated(
                lambda pw: self.win.client.apply_profile(user["uid"], self.preset_key, pw),
                f"{user['username']} → {group.label}"))
            buttons.append(reset)
            push = small_button(f"Update {group.label} from here")
            push.set_tooltip_text(f"Give every account in the {group.label} group "
                                  f"{user['username']}'s settings")
            push.connect("clicked", lambda _b: self._gated(
                lambda pw: self.win.client.save_profile(user["uid"], group.label,
                                                        group.description, pw),
                f"{group.label} updated from {user['username']}"))
            buttons.append(push)
        save = small_button("Save as a group…")
        save.connect("clicked", lambda _b: SavePresetDialog(self.win, user).present(self.win))
        buttons.append(save)
        return buttons

    def _identity_group(self, user: dict) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup()
        custom = self.win.policy.get("custom_profiles", [])
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)

        # The header row: face, name, mode, one line of context. The
        # buttons that used to sit beside it are in the header bar.
        identity = Gtk.Box(spacing=14)
        identity.append(avatar(user["username"], 56))
        names = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, valign=Gtk.Align.CENTER,
                        spacing=4)
        # Name and mode on one line: what this account is called, and what it
        # is allowed to reach, which is the first question a parent has.
        title_row = Gtk.Box(spacing=12)
        name = Gtk.Label(label=user["username"], xalign=0)
        name.add_css_class("title-2")
        title_row.append(name)
        text, protects = labels.mode_badge(user)
        title_row.append(mode_badge(text, protects, big=True))
        names.append(title_row)
        setup = Gtk.Label(label=labels.setup_sentence(self.preset_key, self.drift, custom),
                          xalign=0, wrap=True)
        setup.add_css_class("dim-label")
        names.append(setup)
        identity.append(names)
        box.append(identity)

        # The protections, split by what they mean rather than poured into
        # one wall of seven chips: what is protected on one row, what is
        # left open (amber) on the next, each row led by its word. A
        # parent scanning for "what is open?" reads one row.
        lines = list(labels.protection_lines(user))
        text, protects = labels.time_line(user, self._time_usage(user))
        lines.append(("alarm-symbolic", text, protects))
        for heading, style in (("Protected", "blocking"), ("Left open", "open")):
            items = [(icon, text) for icon, text, p in lines if p == (style == "blocking")]
            if not items:
                continue
            row = Gtk.Box(spacing=10)
            lead = Gtk.Label(label=heading, xalign=0, width_request=76,
                             valign=Gtk.Align.START, margin_top=5)
            lead.add_css_class("caption-heading")
            lead.add_css_class("line-open" if style == "open" else "dim-label")
            row.append(lead)
            chips = Adw.WrapBox(child_spacing=6, line_spacing=6, hexpand=True)
            for icon, text in items:
                chips.append(chip(text, style, icon=icon))
            row.append(chips)
            box.append(row)
        # What was changed here, apart from the group: a note, not a
        # protection, so it does not read as one more thing being blocked.
        notes = labels.drift_sentences(self.drift)
        if notes:
            note = Gtk.Label(label="Changed here: " + " · ".join(notes), xalign=0, wrap=True)
            note.add_css_class("dim-label")
            note.add_css_class("caption")
            note.add_css_class("drift-note")
            box.append(note)
        group.add(box)
        return group

    def _today_groups(self, user: dict) -> list[Adw.PreferencesGroup]:
        groups = []
        waiting = [r for r in self.win.requests if r["uid"] == user["uid"]]
        if waiting:
            group = Adw.PreferencesGroup(title=f"Waiting for you ({len(waiting)})")
            for request in waiting:
                group.add(request_row(self.win, request,
                                      on_answered=lambda _r: self.win.reload()))
            groups.append(group)

        self.blocked_group = Adw.PreferencesGroup(title="Blocked today")
        see_all = small_button("See all", "flat")
        see_all.connect("clicked", lambda _b: self.win.show_activity(user["uid"]))
        self.blocked_group.set_header_suffix(see_all)
        self.blocked_loading = Adw.ActionRow(title="Loading…")
        self.blocked_group.add(self.blocked_loading)
        groups.append(self.blocked_group)

        self.changes_group = Adw.PreferencesGroup(title="Changes to this account")
        self.changes_loading = Adw.ActionRow(title="Loading…")
        self.changes_group.add(self.changes_loading)
        groups.append(self.changes_group)
        self._load_activity(user)
        return groups

    def _load_activity(self, user: dict) -> None:
        since = int(time.time()) - 7 * 86400
        blocked_group, changes_group = self.blocked_group, self.changes_group

        def on_done(events):
            if blocked_group is not self.blocked_group:
                return  # rebuilt since
            blocked_group.remove(self.blocked_loading)
            changes_group.remove(self.changes_loading)
            today = activity_mod.day_start()
            blocks = [e for e in fold(events)
                      if e["kind"] != activity_mod.CHANGE and e["t"] >= today]
            if not blocks:
                blocked_group.add(Adw.ActionRow(
                    title="Nothing blocked today",
                    subtitle="What was allowed through is never recorded."))
            for event in blocks[:6]:
                blocked_group.add(self._blocked_row(user, event))
            if len(blocks) > 6:
                blocked_group.set_title(f"Blocked today ({len(blocks)})")
            changes = [e for e in events if e["kind"] == activity_mod.CHANGE]
            if not changes:
                changes_group.add(Adw.ActionRow(
                    title="No changes this week",
                    subtitle="Who changed what will show here."))
            for event in changes[:5]:
                what, detail = labels.change_sentence(event, self.win.policy)
                row = Adw.ActionRow(title=what, subtitle=detail, use_markup=False)
                row.add_prefix(avatar(event.get("by_username") or "?", 24))
                changes_group.add(row)
            pointer_cursors(blocked_group)

        run_async(lambda: self.win.client.list_activity(since, user["uid"]),
                  on_done, lambda e: self.win.toast(error_text(e)))

    def _blocked_row(self, user: dict, event: dict) -> Adw.ActionRow:
        kind = event["kind"]
        url = event.get("url", "")
        why = labels.why_text(event.get("why", ""))
        times = event.get("times", 1)
        again = f" · {times} times" if times > 1 else ""
        clock = Gtk.Label(label=time.strftime("%H:%M", time.localtime(event["t"])))
        clock.add_css_class("dim-label")
        clock.add_css_class("caption")
        clock.add_css_class("time-label")
        row = Adw.ActionRow(subtitle_lines=2, use_markup=False)
        row.add_prefix(clock)
        if kind == activity_mod.BLOCK:
            row.set_title(labels.short_url(url))
            row.set_subtitle((why or "blocked") + again)
            if not (event.get("why") or "").startswith("rule:"):
                row.add_suffix(allow_menu(self.win, user["uid"], user["username"], url,
                                          on_done=self.win.reload))
        elif kind == activity_mod.PICTURES:
            row.set_title(f"Pictures hidden on {labels.short_url(url)}")
            row.set_subtitle("the page was shown, its pictures were covered" + again)
        elif kind == activity_mod.VIDEO:
            row.set_title(f"Video refused on {labels.short_url(url) or 'a page'}")
            row.set_subtitle("after looking at it" + again)
        elif kind == activity_mod.SEARCH:
            row.set_title(f"Would not search for “{event.get('text', '')}”")
            row.set_subtitle((event.get("why") or "") + again)
        elif kind == activity_mod.TIME:
            row.set_title(labels.time_event_title(event))
            row.set_subtitle(labels.why_text(event.get("why", "")) + again)
        return row

    # -- filtering -----------------------------------------------------------------

    def _filtering_tab(self, user: dict) -> list[Adw.PreferencesGroup]:
        # A whitelist account reaches its approved list and nothing else, so
        # "which kinds of site are blocked" is a question with no meaning
        # there — fifteen toggles that change nothing. The approved list
        # takes their place.
        if user["mode"] == "whitelist":
            return [self._mode_group(user), self._network_group(user), self._approved_sites_group(user),
                    self._pages_group(user)]
        return [self._mode_group(user), self._network_group(user), self._categories_group(user),
                self._pages_group(user)]

    def _approved_sites_group(self, user: dict) -> Adw.PreferencesGroup:
        """The whole of what a whitelist account may reach: ready-made lists
        the family switches on, plus its own."""
        group = Adw.PreferencesGroup(
            title="Approved sites",
            description="This account reaches these and nothing else. Switch on a "
                        "ready-made list rather than typing the thirty hosts a site "
                        "loads from; more than one can be on at once.")
        chosen = set(user.get("whitelist_bundles") or [])
        self.bundle_rows: dict[str, Adw.SwitchRow] = {}
        self._bundles_building = True
        loading = Adw.ActionRow(title="Loading the ready-made lists…")
        group.add(loading)

        domains = user.get("whitelist", [])
        own = Adw.ActionRow(
            title="This family's own list",
            subtitle=(labels.plural(len(domains), "site") + " added here"
                      if domains else "Nothing added here yet"),
            activatable=True)
        own.add_suffix(Gtk.Image(icon_name="go-next-symbolic"))
        own.connect("activated", lambda _r: WhitelistDialog(
            self.win, f"Approved sites — {user['username']}", domains,
            lambda new: self._gated(
                lambda pw: self.win.client.set_whitelist(user["uid"], new, pw),
                f"Approved sites saved for {user['username']}")).present(self.win))

        def on_done(bundles):
            group.remove(loading)
            for bundle in bundles:
                row = Adw.SwitchRow(
                    title=bundle["label"],
                    subtitle=bundle["description"] + f" ({bundle['domains']} sites)",
                    subtitle_lines=4, active=bundle["key"] in chosen)
                row.connect("notify::active", self._on_bundle, bundle["key"])
                self.bundle_rows[bundle["key"]] = row
                group.add(row)
            group.add(own)
            self._bundles_building = False
            pointer_cursors(group)

        run_async(self.win.client.list_whitelist_bundles, on_done,
                  lambda e: (group.remove(loading), group.add(own),
                             self.win.toast(error_text(e))))
        return group

    def _on_bundle(self, row, _param, key: str) -> None:
        if self._bundles_building:
            return
        chosen = sorted(k for k, r in self.bundle_rows.items() if r.get_active())
        user = self.user
        if chosen == sorted(user.get("whitelist_bundles") or []):
            return
        self._gated(
            lambda pw: self.win.client.set_whitelist_bundles(user["uid"], chosen, pw),
            (f"{self.bundle_rows[key].get_title()} "
             + ("added to" if row.get_active() else "removed from")
             + f" {user['username']}'s approved sites"))

    def _mode_group(self, user: dict) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup(title="How the internet is filtered")
        custom = self.win.policy.get("custom_profiles", [])
        groups = profiles_mod.all_profiles(custom)
        keys = [g.key for g in groups]
        current = user.get("profile") if user.get("profile") in keys else None
        # The family's groups, then No group. Nothing ready-made: "Child"
        # means something different in every home, so the family names its
        # own, and an account is in one of theirs or in none.
        profile_row = Adw.ComboRow(
            title="Group",
            model=Gtk.StringList.new([g.label for g in groups] + [labels.NO_GROUP]))
        profile_row.set_selected(keys.index(current) if current else len(keys))
        if current:
            profile_hint = (profiles_mod.get(current, custom).description
                            or f"This account follows the {profiles_mod.get(current, custom).label} "
                               "group: change the group and it changes too.")
        elif groups:
            profile_hint = ("Not in a group. Pick one to give this account the group's "
                            "settings and have it follow the group from then on.")
        else:
            profile_hint = ("No groups yet. Tune this account, then Save as a group… "
                            "in the header to name its settings and give them to others.")

        def on_profile(combo, _p):
            index = combo.get_selected()
            key = keys[index] if index < len(keys) else ""
            if key == (current or ""):
                return
            if key:
                self._gated(lambda pw: self.win.client.apply_profile(user["uid"], key, pw),
                            f"{user['username']} → {profiles_mod.get(key, custom).label}")
            else:
                self._gated(lambda pw: self.win.client.apply_profile(user["uid"], "", pw),
                            f"{user['username']} is in no group; settings kept")

        profile_row.connect("notify::selected", on_profile)
        group.add(profile_row)

        if current:
            delete_row = Adw.ButtonRow(title="Delete This Group…")
            delete_row.add_css_class("destructive-action")
            delete_row.connect("activated", lambda *_: self._confirm_delete_preset(current))
            group.add(delete_row)

        mode_row = Adw.ComboRow(title="Filter mode",
                                model=Gtk.StringList.new([labels.MODE_LABELS[m] for m in MODES]))
        mode_row.set_selected(MODES.index(user["mode"]))

        def on_mode(combo, _p):
            new_mode = MODES[combo.get_selected()]
            if new_mode == user["mode"]:
                return
            self._gated(lambda pw: self.win.client.set_filter_mode(user["uid"], new_mode, pw),
                        f"{user['username']} → {labels.MODE_LABELS[new_mode]}")

        mode_row.connect("notify::selected", on_mode)
        group.add(mode_row)
        hint_under(group, profile_hint)
        hint_under(group, "Filter mode: " + labels.MODE_HINTS.get(user["mode"], ""))
        return group

    def _network_group(self, user: dict) -> Adw.PreferencesGroup:
        """What the account may reach that is not the web.

        Two settings, both plain about what they mean. Video calls are UDP
        the filter cannot read; the switch is on by default so school calls
        work without a visit here, and the sentence says what "on" costs.
        Extra ports are the advanced escape hatch for one program that is
        neither web nor mail; everything not listed is refused.
        """
        group = Adw.PreferencesGroup(
            title="Beyond the web",
            description="Web pages go through the filter whatever port they use. "
                        "These two are the exceptions.")
        if user["mode"] not in ("filtered", "dnsfilter"):
            group.add(Adw.ActionRow(
                title="Not in this mode",
                subtitle="Whitelist only and No internet allow nothing beyond the "
                         "approved sites, so there is nothing to set here.",
                subtitle_lines=2))
            return group

        video = Adw.SwitchRow(
            title="Video calls",
            subtitle="Zoom, Meet and the like send picture and sound over a channel "
                     "the filter cannot read. Leave this on for an account that has "
                     "calls to make; turn it off for one that does not.",
            subtitle_lines=4,
            active=user.get("video_calls", True))

        def on_video(row, _param):
            wanted = row.get_active()
            if wanted == user.get("video_calls", True):
                return
            self._gated(lambda pw: self.win.client.set_network_access(
                user["uid"], wanted, list(user.get("extra_ports", [])), pw),
                f"{user['username']}: video calls {'on' if wanted else 'off'}")

        video.connect("notify::active", on_video)
        group.add(video)

        ports_row = Adw.EntryRow(title="Extra ports (advanced)", show_apply_button=True,
                                 text=", ".join(str(p) for p in user.get("extra_ports", [])))

        def on_apply(row):
            text = row.get_text().strip()
            try:
                ports = sorted({int(p) for p in text.replace(" ", "").split(",") if p})
            except ValueError:
                self.win.toast("Ports are numbers separated by commas, like 22, 2222")
                return
            self._gated(lambda pw: self.win.client.set_network_access(
                user["uid"], user.get("video_calls", True), ports, pw),
                f"{user['username']}: extra ports "
                + (", ".join(map(str, ports)) if ports else "cleared"))

        ports_row.connect("apply", on_apply)
        group.add(ports_row)
        hint_under(group, "Ports a program on this account needs that are not web or "
                          "mail — 22 for ssh, say. Everything not listed is refused. "
                          "Leave this empty unless something specific stopped working.")
        return group

    def _categories_group(self, user: dict) -> Adw.PreferencesGroup:
        """The category chooser, on the page instead of behind a row.

        Two columns, so fifteen rows fit without scrolling. Categories come
        from the list bundle on the machine, so the choices shown are the
        ones actually enforceable.
        """
        self.chosen = set(user.get("blocked_categories", []))
        self._cats_saved = sorted(self.chosen)
        self.checks: dict[str, Gtk.CheckButton] = {}
        self._building = True
        group = Adw.PreferencesGroup(title="Blocked kinds of sites")
        if user["mode"] == "unfiltered":
            group.set_description("An unfiltered account blocks nothing.")
            group.set_sensitive(False)
        elif user["mode"] == "none":
            group.set_description(labels.MODE_NOTHING_APPLIES["none"])
            group.set_sensitive(False)

        suffix = Gtk.Box(spacing=6)
        custom = self.win.policy.get("custom_profiles", [])
        if self.preset_key:
            label = profiles_mod.get(self.preset_key, custom).label
            preset_btn = small_button(f"{label} group")
            preset_btn.set_tooltip_text(f"Block what the {label} group blocks")
            preset_btn.connect("clicked", lambda _b: self._set_categories(
                set(profiles_mod.get(self.preset_key, custom).blocked_categories)))
            suffix.append(preset_btn)
        all_btn = small_button("All", "flat")
        all_btn.connect("clicked", lambda _b: self._set_categories(set(self.checks)))
        none_btn = small_button("None", "flat")
        none_btn.connect("clicked", lambda _b: self._set_categories(set()))
        suffix.append(all_btn)
        suffix.append(none_btn)
        group.set_header_suffix(suffix)

        self.grid = Gtk.Grid(column_homogeneous=True, column_spacing=6, row_spacing=2)
        self.grid.add_css_class("card")
        self.grid.set_margin_top(2)
        self.grid.set_margin_bottom(2)
        group.add(self.grid)
        loading = Gtk.Label(label="Loading the list…", margin_top=12, margin_bottom=12)
        loading.add_css_class("dim-label")
        self.grid.attach(loading, 0, 0, 2, 1)

        added = {name for change in self.drift if change.get("field") == "blocked_categories"
                 for name in change.get("added", [])}
        floor = default_blocks()
        adblock = self.win.policy.get("adblock", {}).get("enabled", True)
        grid = self.grid

        def on_done(info):
            if grid is not self.grid:
                return
            clear(grid)
            n = len(info["categories"])
            rows_per_column = (n + 1) // 2
            for index, category in enumerate(info["categories"]):
                name = category["name"]
                check = Gtk.CheckButton(active=name in self.chosen, margin_start=8,
                                        margin_end=8, margin_top=4, margin_bottom=4)
                line = Gtk.Box(spacing=8, hexpand=True)
                text = Gtk.Label(label=category["label"], xalign=0, hexpand=True,
                                 ellipsize=3)
                line.append(text)
                if name in added:
                    line.append(tag("added here", "acc"))
                    check.add_css_class("added-here")
                elif name == "ads" and adblock:
                    line.append(tag("for everyone"))
                elif name in floor:
                    line.append(tag("on by default"))
                check.set_child(line)
                check.connect("toggled", self._on_category, name)
                self.checks[name] = check
                grid.attach(check, index // rows_per_column, index % rows_per_column, 1, 1)
            group.set_description(
                f"{info['domains']:,} sites are classified. Sites not on the list are "
                "judged by the other settings, so this is a floor, not a guarantee.")
            self._building = False
            pointer_cursors(grid)

        run_async(self.win.client.list_categories, on_done,
                  lambda e: self.win.toast(error_text(e)))
        return group

    def _on_category(self, check: Gtk.CheckButton, name: str) -> None:
        if self._building:
            return
        if check.get_active():
            self.chosen.add(name)
        else:
            self.chosen.discard(name)
        self._save_categories()

    def _set_categories(self, wanted: set[str]) -> None:
        self._building = True
        for name, check in self.checks.items():
            check.set_active(name in wanted)
        self._building = False
        self.chosen = set(wanted) & set(self.checks) if self.checks else set(wanted)
        self._save_categories()

    def _save_categories(self) -> None:
        user = self.user
        chosen = sorted(self.chosen)
        if chosen == self._cats_saved:
            return
        self._cats_saved = chosen
        self._gated(lambda pw: self.win.client.set_blocked_categories(user["uid"], chosen, pw),
                    f"{labels.plural(len(chosen), 'kind')} of site blocked for {user['username']}")

    def _pages_group(self, user: dict) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup(title="Particular pages")
        rules = user.get("rules", [])
        allows = [r["pattern"] for r in rules if r.get("action") == "allow"]
        blocks = [r["pattern"] for r in rules if r.get("action") == "block"]
        parts = []
        if allows:
            parts.append("allows " + ", ".join(allows[:3]) + ("…" if len(allows) > 3 else ""))
        if blocks:
            parts.append("blocks " + ", ".join(blocks[:3]) + ("…" if len(blocks) > 3 else ""))
        summary = (f"{labels.plural(len(rules), 'rule')}, checked top to bottom"
                   + (": " + "; ".join(parts) if parts else "")) if rules else \
            "No rules. Every page not blocked by the settings above is allowed."
        rules_row = Adw.ActionRow(title="Page rules", subtitle=summary, subtitle_lines=3,
                                  activatable=True, use_markup=False)
        edit = small_button("Edit")
        rules_row.add_suffix(edit)
        open_rules = lambda *_: RulesDialog(  # noqa: E731
            self.win, f"Page rules — {user['username']}", rules,
            lambda new: self._gated(
                lambda pw: self.win.client.set_url_rules(user["uid"], new, pw),
                f"Page rules saved for {user['username']}")).present(self.win)
        edit.connect("clicked", open_rules)
        rules_row.connect("activated", open_rules)
        if user["mode"] != "filtered":
            rules_row.set_sensitive(False)
            rules_row.set_subtitle("Only used in “Filtered internet” mode, the one "
                                   "mode that can see which page is being asked for.")
        group.add(rules_row)

        if user["mode"] != "whitelist":
            # In every other mode the approved list is inert; say so once,
            # here, rather than showing an editor that changes nothing.
            wl_row = Adw.ActionRow(
                title="Approved sites",
                subtitle="Only a Whitelist only account is limited to a list.")
            wl_row.set_sensitive(False)
            group.add(wl_row)
        return group

    # -- pictures and words --------------------------------------------------------

    def _media_tab(self, user: dict) -> list[Adw.PreferencesGroup]:
        group = Adw.PreferencesGroup(
            title="Pages and pictures",
            description="How much of the web's imagery and language this "
                        "account sees.")
        media_row = Adw.ComboRow(
            title="Pictures and video", use_subtitle=True,
            model=Gtk.StringList.new([labels.MEDIA_LABELS[m] for m in MEDIA_LEVELS]))
        level = user.get("media_level", "none")
        media_row.set_selected(MEDIA_LEVELS.index(level) if level in MEDIA_LEVELS else 0)
        media_hint = labels.MEDIA_HINTS.get(level, "")

        def on_media(combo, _p):
            new_level = MEDIA_LEVELS[combo.get_selected()]
            if new_level == user.get("media_level", "none"):
                return
            self._gated(lambda pw: self.win.client.set_media_level(user["uid"], new_level, pw),
                        f"Pictures: {labels.MEDIA_LABELS[new_level].lower()}")

        media_row.connect("notify::selected", on_media)
        # NOT greyed out outside filtered mode: this setting also decides
        # whether image search results are shown at all, in every mode.
        # Disabling a control that still acts is worse than a wordy subtitle.
        if user["mode"] in ("none", "unfiltered"):
            media_row.set_sensitive(False)
            media_row.set_use_subtitle(False)
            media_row.set_subtitle(labels.MODE_NOTHING_APPLIES[user["mode"]])
            media_hint = ""
        elif user["mode"] != "filtered":
            media_hint = ("Pictures on pages are not checked in this mode — only "
                          "“Filtered internet” can see them. This still decides "
                          "whether image search results are shown.")
        group.add(media_row)

        cover_row = Adw.ComboRow(
            title="Covered pictures look like", use_subtitle=True,
            model=Gtk.StringList.new([labels.COVER_LABELS[k] for k in labels.COVER_ORDER]))
        style = user.get("cover_style", "frost")
        cover_row.set_selected(labels.COVER_ORDER.index(style)
                               if style in labels.COVER_ORDER else 0)
        cover_hint = labels.COVER_HINTS.get(style, "")

        def on_cover(combo, _p):
            new_style = labels.COVER_ORDER[combo.get_selected()]
            if new_style == user.get("cover_style", "frost"):
                return
            self.win.call(
                lambda: self.win.client.set_cover_style(user["uid"], new_style),
                done_msg=f"Covered pictures: {labels.COVER_LABELS[new_style].lower()}")

        cover_row.connect("notify::selected", on_cover)
        if user["mode"] in ("none", "unfiltered") or level in ("none", "all"):
            cover_row.set_sensitive(False)
            cover_row.set_use_subtitle(False)
            cover_row.set_subtitle("Only matters when pictures are checked and "
                                   "partly covered.")
            cover_hint = ""
        group.add(cover_row)

        language_row = Adw.ComboRow(
            title="Bad language", use_subtitle=True,
            model=Gtk.StringList.new([labels.LANGUAGE_LABELS[m] for m in labels.LANGUAGE_ORDER]))
        setting = user.get("language_filter", "off")
        language_row.set_selected(labels.LANGUAGE_ORDER.index(setting)
                                  if setting in labels.LANGUAGE_ORDER else 0)
        language_hint = ("Replacing reads better than blocking: a page that reads "
                         "normally minus the language beats one that refuses to load.")

        def on_language(combo, _p):
            new_setting = labels.LANGUAGE_ORDER[combo.get_selected()]
            if new_setting == user.get("language_filter", "off"):
                return
            self._gated(lambda pw: self.win.client.set_language_filter(
                user["uid"], new_setting, pw), labels.LANGUAGE_LABELS[new_setting])

        language_row.connect("notify::selected", on_language)
        if user["mode"] in ("none", "unfiltered"):
            language_row.set_sensitive(False)
            language_row.set_use_subtitle(False)
            language_row.set_subtitle(labels.MODE_NOTHING_APPLIES[user["mode"]])
            language_hint = ""
        elif user["mode"] != "filtered":
            language_hint = ("Pages are not rewritten in this mode — only “Filtered "
                             "internet” can read them. This still blocks searches "
                             "containing bad language.")
        group.add(language_row)
        for title, hint in (("Pictures and video", media_hint),
                            ("Covered pictures", cover_hint),
                            ("Bad language", language_hint)):
            if hint:
                hint_under(group, f"{title}: {hint}")
        return [group]

    # -- youtube -------------------------------------------------------------------

    def _youtube_tab(self, user: dict) -> list[Adw.PreferencesGroup]:
        """What this person may watch on YouTube.

        Restricted Mode on its own is far too coarse for a family that
        wants shiurim but not entertainment — it is one switch for the
        whole site. Categories and an approved-channel list are what make
        it usable. Every change saves at once.
        """
        settings = dict(user.get("youtube") or {})
        self.yt_blocked = set(settings.get("blocked_categories", []))
        self.yt_channels = list(settings.get("allowed_channels", []))
        self.yt_names = dict(settings.get("channel_names", {}))
        self.yt_restrict = settings.get("restrict", "moderate")
        self._yt_saved = self._youtube_settings()
        self._yt_building = True
        groups = []

        general = Adw.PreferencesGroup(
            title="Restricted Mode",
            description="YouTube's own filter, applied to every request. It "
                        "is coarse on its own, which is what the settings "
                        "below are for.")
        if user["mode"] != "filtered":
            # Genuinely inert here: nothing outside the proxy can see which
            # video is playing.
            general.set_description(
                "Only used in “Filtered internet” mode; every other mode is "
                "enforced at the connection, which cannot see which video "
                "is playing.")
        restrict_row = Adw.ComboRow(
            title="Restricted Mode",
            model=Gtk.StringList.new(
                [labels.YOUTUBE_RESTRICT_LABELS[r] for r in labels.YOUTUBE_RESTRICT_ORDER]))
        restrict_row.set_selected(labels.YOUTUBE_RESTRICT_ORDER.index(self.yt_restrict)
                                  if self.yt_restrict in labels.YOUTUBE_RESTRICT_ORDER else 1)

        def on_restrict(combo, _p):
            self.yt_restrict = labels.YOUTUBE_RESTRICT_ORDER[combo.get_selected()]
            self._save_youtube()

        restrict_row.connect("notify::selected", on_restrict)
        general.add(restrict_row)
        groups.append(general)

        channels_group = Adw.PreferencesGroup(
            title="Approved channels",
            description="If this list has anything in it, only these "
                        "channels may be watched and every category setting "
                        "below stops mattering.")
        add = Gtk.Button(icon_name="list-add-symbolic", valign=Gtk.Align.CENTER)
        add.connect("clicked", lambda _b: self._add_channel())
        channels_group.set_header_suffix(add)
        self.channel_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.channel_list.add_css_class("boxed-list")
        channels_group.add(self.channel_list)
        groups.append(channels_group)
        self._rebuild_channels()

        categories = Adw.PreferencesGroup(
            title="Blocked kinds of video",
            description="Turn one on to block that kind of video for this "
                        "person. YouTube labels every video with one of these.")
        bulk = Gtk.Box(spacing=6)
        self.kind_switches: dict[str, Adw.SwitchRow] = {}
        all_btn = small_button("All", "flat")
        all_btn.connect("clicked", lambda _b: self._set_all_kinds(True))
        none_btn = small_button("None", "flat")
        none_btn.connect("clicked", lambda _b: self._set_all_kinds(False))
        bulk.append(all_btn)
        bulk.append(none_btn)
        categories.set_header_suffix(bulk)
        for code, label in sorted(YOUTUBE_CATEGORIES.items(), key=lambda kv: kv[1]):
            row = Adw.SwitchRow(title=label, active=code in self.yt_blocked,
                                use_markup=False)
            row.connect("notify::active", self._toggle_kind, code)
            self.kind_switches[code] = row
            categories.add(row)
        groups.append(categories)
        live = user["mode"] == "filtered"
        for group in groups:
            group.set_sensitive(live)
        restrict_row.set_sensitive(live)
        for row in self.kind_switches.values():
            row.set_sensitive(live)
        self._yt_building = False
        return groups

    def _youtube_settings(self) -> dict:
        settings = {"restrict": self.yt_restrict}
        if self.yt_blocked:
            settings["blocked_categories"] = sorted(self.yt_blocked)
        if self.yt_channels:
            # A copy: the saved settings are compared with the next ones,
            # and sharing the list made every approval after the first
            # look like no change at all, so it was never saved.
            settings["allowed_channels"] = list(self.yt_channels)
            names = {c: self.yt_names[c] for c in self.yt_channels if c in self.yt_names}
            if names:
                settings["channel_names"] = names
        return settings

    def _save_youtube(self) -> None:
        if self._yt_building:
            return
        user = self.user
        settings = self._youtube_settings()
        if settings == self._yt_saved:
            return
        self._yt_saved = settings
        self._gated(lambda pw: self.win.client.set_youtube(user["uid"], settings, pw),
                    f"YouTube settings saved for {user['username']}")

    def _toggle_kind(self, row, _param, code: str) -> None:
        if row.get_active():
            self.yt_blocked.add(code)
        else:
            self.yt_blocked.discard(code)
        self._save_youtube()

    def _set_all_kinds(self, on: bool) -> None:
        self._yt_building = True
        for row in self.kind_switches.values():
            row.set_active(on)
        self._yt_building = False
        self.yt_blocked = set(self.kind_switches) if on else set()
        self._save_youtube()

    def _add_channel(self) -> None:
        def on_pick(ref: str, name: str | None) -> None:
            if ref in self.yt_channels:
                return
            self.yt_channels.append(ref)
            if name:
                self.yt_names[ref] = name
            self._rebuild_channels()
            self._save_youtube()

        ApproveChannelDialog(self.win, self.yt_channels, on_pick).present(self.win)

    def _remove_channel(self, ref: str) -> None:
        self.yt_channels.remove(ref)
        self.yt_names.pop(ref, None)
        self._rebuild_channels()
        self._save_youtube()

    def _rebuild_channels(self) -> None:
        clear(self.channel_list)
        if not self.yt_channels:
            self.channel_list.append(Adw.ActionRow(
                title="Every channel is allowed",
                subtitle="Only the settings below apply."))
            return
        for ref in self.yt_channels:
            # A channel found by search is listed by its ID, which nobody
            # can read, so its name leads and the ID is the subtitle.
            name = self.yt_names.get(ref)
            row = Adw.ActionRow(title=name or ref, subtitle=ref if name else "",
                                use_markup=False)
            remove = Gtk.Button(icon_name="user-trash-symbolic", valign=Gtk.Align.CENTER)
            remove.add_css_class("flat")
            remove.connect("clicked", lambda _b, r=ref: self._remove_channel(r))
            row.add_suffix(remove)
            self.channel_list.append(row)

    # -- time --------------------------------------------------------------------

    def _time_usage(self, user: dict) -> dict | None:
        return (getattr(self.win, "time_usage", None) or {}).get(str(user["uid"]))

    def _time_tab(self, user: dict) -> list[Adw.PreferencesGroup]:
        """How long, and when, this person may use the computer.

        A daily limit with one-click amounts and a calendar to paint the
        allowed hours on. Nothing is set until the parent sets it, and an
        administrator's page says plainly that it cannot be.
        """
        settings = dict(user.get("time") or {})
        usage = self._time_usage(user)
        self._time_saved = timelimits.parse(settings)
        self._time_minutes = timelimits.daily_minutes(settings)
        self._time_grid = timelimits.grid(settings)
        self._time_building = True
        self._time_debounce = None

        today = Adw.PreferencesGroup(title=labels.TIME_TODAY_TITLE,
                                     description=labels.TIME_TAB_INTRO)
        text, _protects = labels.time_line(user, usage)
        subtitle = ""
        if usage and usage.get("block_ends"):
            subtitle = "Allowed until " + time.strftime("%H:%M", time.localtime(usage["block_ends"]))
        elif usage and usage.get("blocked"):
            subtitle = "Not allowed right now"
            comes_back = usage.get("next_allowed")
            if comes_back and comes_back > time.time():
                subtitle += ", until " + labels.until_text(comes_back)
        if usage and usage.get("signed_in"):
            subtitle = (subtitle + " · " if subtitle else "") + "Signed in now"
        self.time_today_row = Adw.ActionRow(title=text, subtitle=subtitle, use_markup=False)
        self.time_today_row.add_prefix(Gtk.Image(icon_name="alarm-symbolic"))
        today.add(self.time_today_row)
        if user.get("admin"):
            today.add(Adw.ActionRow(title="Administrator", subtitle=labels.TIME_ADMIN_NOTE,
                                    subtitle_lines=4))
            self._time_building = False
            return [today]

        limit = Adw.PreferencesGroup(title="Time each day")
        choices = [labels.TIME_LIMIT_LABELS[m] for m in labels.TIME_LIMIT_ORDER]
        self.time_limit_row = Adw.ComboRow(
            title="Daily limit", model=Gtk.StringList.new(choices + [labels.TIME_LIMIT_CUSTOM]))
        preset = self._time_minutes in labels.TIME_LIMIT_ORDER
        self.time_limit_row.set_selected(
            labels.TIME_LIMIT_ORDER.index(self._time_minutes) if preset else len(choices))
        self.time_limit_row.connect("notify::selected", self._on_time_limit_choice)
        limit.add(self.time_limit_row)
        self.time_custom_row = Adw.SpinRow.new_with_range(5, timelimits.MAX_DAILY_MINUTES, 5)
        self.time_custom_row.set_title("Minutes a day")
        self.time_custom_row.set_value(self._time_minutes or 90)
        self.time_custom_row.set_visible(not preset)
        self.time_custom_row.connect("notify::value", self._on_time_custom)
        limit.add(self.time_custom_row)
        hint_under(limit, labels.TIME_LIMIT_HINT)

        schedule = Adw.PreferencesGroup(title=labels.SCHEDULE_TITLE)
        # The presets sit on their own row under the title: beside it they
        # squeezed the title down to "When this …".
        presets = Gtk.Box(spacing=6, margin_bottom=6)
        for key in labels.SCHEDULE_PRESET_ORDER:
            button = small_button(labels.SCHEDULE_PRESET_LABELS[key], "flat")
            button.set_tooltip_text(labels.SCHEDULE_PRESET_HINTS[key])
            button.connect("clicked", lambda _b, k=key: self._set_schedule(
                timelimits.SCHEDULE_PRESETS[k]))
            presets.append(button)
        schedule.add(presets)
        self.schedule_grid = ScheduleGrid(self._time_grid, on_change=self._on_schedule_painted)
        frame = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        frame.add_css_class("card")
        frame.append(self.schedule_grid)
        schedule.add(frame)
        schedule.add(legend())
        hint_under(schedule, labels.SCHEDULE_HINT)
        self._time_building = False
        return [today, limit, schedule]

    def _on_time_limit_choice(self, combo, _param) -> None:
        if self._time_building:
            return
        index = combo.get_selected()
        if index >= len(labels.TIME_LIMIT_ORDER):
            self.time_custom_row.set_visible(True)
            self._time_minutes = int(self.time_custom_row.get_value())
        else:
            self.time_custom_row.set_visible(False)
            self._time_minutes = labels.TIME_LIMIT_ORDER[index]
        self._save_time()

    def _on_time_custom(self, row, _param) -> None:
        """A spin row fires on every click; save once the clicking stops."""
        if self._time_building or not row.get_visible():
            return
        self._time_minutes = int(row.get_value())
        if self._time_debounce:
            GLib.source_remove(self._time_debounce)

        def fire():
            self._time_debounce = None
            self._save_time()
            return False

        self._time_debounce = GLib.timeout_add(700, fire)

    def _set_schedule(self, grid: list[str]) -> None:
        self.schedule_grid.set_grid(grid)
        self._on_schedule_painted(list(grid))

    def _on_schedule_painted(self, grid: list[str]) -> None:
        self._time_grid = list(grid)
        self._save_time()

    def _save_time(self) -> None:
        if self._time_building:
            return
        user = self.user
        settings = timelimits.parse({"daily_minutes": self._time_minutes,
                                     "allowed": self._time_grid})
        if settings == self._time_saved:
            return
        self._time_saved = settings
        self._gated(lambda pw: self.win.client.set_time_limits(user["uid"], settings, pw),
                    f"{user['username']}: {labels.time_summary(settings)}")

    # -- apps ----------------------------------------------------------------------

    # What each choice of app access means, under the row (see hint_under).
    ACCESS_HINTS = {
        "approved": "Only the apps on the approved list, which is kept under Apps in "
                    "the sidebar. The strict choice, and what every account starts with.",
        "store": "Everything on Flathub that is not blocked below. Apps rated for "
                 "nudity, sexual themes, bad language, gambling, drugs or graphic "
                 "violence are left out unless an administrator approves them by name.",
    }

    def _apps_tab(self, user: dict) -> list[Adw.PreferencesGroup]:
        """Apps on this computer, from one user's point of view.

        Three things decide what this account may have: which list the
        Store shows it (the approved list, or the whole store), which
        kinds of app are blocked, and which single apps are blocked. The
        blocks apply whichever list is chosen, and to running as well as
        installing. Apps install system-wide, so 'uninstall' removes an
        app for everyone — that is spelled out.
        """
        self.blocked_kinds: set[str] = set(user.get("blocked_app_kinds") or [])
        self.blocked_apps: list[str] = sorted(user.get("blocked_apps") or [])
        self._kinds_saved = sorted(self.blocked_kinds)

        installs_group = Adw.PreferencesGroup(title="What this account may install")
        installs = Adw.SwitchRow(title="Can install apps",
                                 subtitle="From the KosherOS Store, within the limits below",
                                 active=user.get("can_install_apps", True))
        installs.connect("notify::active", lambda s, _p: (
            s.get_active() != user.get("can_install_apps", True) and self.win.call(
                lambda: self.win.client.set_user_can_install(user["uid"], s.get_active()),
                done_msg=f"App installs {'enabled' if s.get_active() else 'disabled'} for {user['username']}")))
        installs_group.add(installs)

        access_row = Adw.ComboRow(title="Which apps")
        access_row.set_model(Gtk.StringList.new(
            [appaccess.APP_ACCESS_LABELS[key] for key in appaccess.APP_ACCESS]))
        current = appaccess.access_of(user)
        access_row.set_selected(appaccess.APP_ACCESS.index(current))
        installs_group.add(access_row)
        hint = hint_under(installs_group, self.ACCESS_HINTS[current])

        def on_access(combo, _p):
            key = appaccess.APP_ACCESS[combo.get_selected()]
            hint.set_label(self.ACCESS_HINTS[key])
            if key == appaccess.access_of(user):
                return
            user["app_access"] = key
            self._gated(lambda pw: self.win.client.set_user_app_access(user["uid"], key, pw),
                        f"{user['username']}: {appaccess.APP_ACCESS_LABELS[key].lower()}")

        access_row.connect("notify::selected", on_access)

        kinds_group = Adw.PreferencesGroup(
            title="Blocked kinds of apps",
            description="Blocked whichever list is chosen above: not offered in the "
                        "Store, and hidden from this account if already installed.")
        self.kind_rows: dict[str, Adw.SwitchRow] = {}
        for key in appkinds.KIND_KEYS:
            row = Adw.SwitchRow(title=appkinds.label(key), active=key in self.blocked_kinds,
                                use_markup=False)
            row.connect("notify::active", self._on_kind, key)
            self.kind_rows[key] = row
            kinds_group.add(row)

        self.blocked_apps_group = Adw.PreferencesGroup(
            title="Blocked apps",
            description="Single apps this account may not have, whatever else says. "
                        "Search to block an app that is not installed yet.")
        self.blocked_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.blocked_list.add_css_class("boxed-list")
        self.blocked_apps_group.add(self.blocked_list)
        self.block_entry = Adw.EntryRow(title="Block an app", margin_top=6)
        self.block_entry.set_show_apply_button(True)
        self.block_entry.connect("apply", lambda _e: self._search_to_block(user))
        self.block_entry.connect("entry-activated", lambda _e: self._search_to_block(user))
        self.blocked_apps_group.add(self.block_entry)
        self.block_results = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, margin_top=6,
                                         visible=False)
        self.block_results.add_css_class("boxed-list")
        self.blocked_apps_group.add(self.block_results)
        self._render_blocked(user)

        self.apps_group = Adw.PreferencesGroup(
            title="Installed apps",
            description="Turning an app off blocks it for this account only. "
                        "Uninstalling removes it from the whole computer.")
        self.apps_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.apps_list.add_css_class("boxed-list")
        self.apps_list.append(Adw.ActionRow(title="Loading…"))
        self.apps_group.add(self.apps_list)
        self._load_apps(user)
        return [installs_group, kinds_group, self.blocked_apps_group, self.apps_group]

    # -- kinds --

    def _on_kind(self, row: Adw.SwitchRow, _param, key: str) -> None:
        if row.get_active():
            self.blocked_kinds.add(key)
        else:
            self.blocked_kinds.discard(key)
        kinds = sorted(self.blocked_kinds)
        if kinds == self._kinds_saved:
            return
        self._kinds_saved = kinds
        user = self.user
        user["blocked_app_kinds"] = kinds
        self._gated(lambda pw: self.win.client.set_user_blocked_app_kinds(user["uid"], kinds, pw),
                    f"{labels.plural(len(kinds), 'kind')} of app blocked for {user['username']}")

    # -- single apps --

    def _render_blocked(self, user: dict) -> None:
        clear(self.blocked_list)
        if not self.blocked_apps:
            row = Adw.ActionRow(title="No single apps blocked")
            row.set_sensitive(False)
            self.blocked_list.append(row)
        for ref in self.blocked_apps:
            row = Adw.ActionRow(title=labels.app_name(ref), subtitle=ref, use_markup=False)
            unblock = small_button("Unblock", "flat")
            unblock.set_valign(Gtk.Align.CENTER)
            unblock.connect("clicked", lambda _b, r=ref: self._set_blocked(user, r, False))
            row.add_suffix(unblock)
            self.blocked_list.append(row)
        pointer_cursors(self.blocked_list)

    def _set_blocked(self, user: dict, ref: str, blocked: bool) -> None:
        refs = set(self.blocked_apps)
        (refs.add if blocked else refs.discard)(ref)
        self.blocked_apps = sorted(refs)
        user["blocked_apps"] = list(self.blocked_apps)
        self._render_blocked(user)
        self._gated(lambda pw: self.win.client.set_user_blocked_apps(
            user["uid"], list(self.blocked_apps), pw),
            f"{labels.app_name(ref)} {'blocked' if blocked else 'unblocked'} for {user['username']}")
        if self.apps_list is not None:
            self._load_apps(user)

    def _search_to_block(self, user: dict) -> None:
        query = self.block_entry.get_text().strip()
        if not query:
            return
        results = self.block_results
        results.set_visible(True)
        clear(results)
        results.append(Adw.ActionRow(title="Searching…", sensitive=False))

        def on_done(found):
            if results is not self.block_results:
                return
            clear(results)
            shown = [a for a in found if a["ref"] not in self.blocked_apps][:20]
            if not shown:
                results.append(Adw.ActionRow(title="No matches", sensitive=False))
                return
            for app in shown:
                row = Adw.ActionRow(title=app["name"], subtitle=app.get("summary") or app["ref"],
                                    use_markup=False)
                btn = small_button("Block", "destructive-action")
                btn.set_valign(Gtk.Align.CENTER)
                btn.connect("clicked", lambda _b, a=app: (
                    self._set_blocked(user, a["ref"], True),
                    results.set_visible(False), self.block_entry.set_text("")))
                row.add_suffix(btn)
                results.append(row)
            pointer_cursors(results)

        run_async(lambda: self.win.client.search_apps(query), on_done,
                  lambda e: (clear(results),
                             results.append(Adw.ActionRow(title=error_text(e), sensitive=False))))

    # -- installed apps --

    def _load_apps(self, user: dict) -> None:
        apps_list = self.apps_list

        def on_done(details):
            if apps_list is not self.apps_list:
                return
            clear(apps_list)
            if not details:
                row = Adw.ActionRow(title="No apps installed yet")
                row.set_sensitive(False)
                apps_list.append(row)
                return
            for app in details:
                apps_list.append(self._app_row(user, app))
            pointer_cursors(apps_list)

        run_async(self.win.client.list_installed_details, on_done,
                  lambda e: self.win.toast(error_text(e)))

    def _app_row(self, user: dict, app: dict) -> Adw.ActionRow:
        subtitle = app["ref"]
        if app.get("installed_by"):
            subtitle += f" · installed by {app['installed_by']}"
        row = Adw.ActionRow(title=app["name"], subtitle=subtitle, use_markup=False)
        # Why this account cannot run the app, if it cannot: the switch
        # answers for a single block; anything else is named so the parent
        # knows which setting to change.
        approved = {app["ref"]} if app.get("approved", True) else set()
        reason = appaccess.decide(user, app, approved)
        if reason and reason != "blocked":
            row.add_suffix(tag({"kind": f"{appkinds.label(app.get('kind', 'other'))} blocked",
                                "not-approved": "not approved",
                                "content": "above the content ceiling",
                                "circumvention": "gets around the filter",
                                "unknown": "not in the app list"}.get(reason, reason)))
        switch = Gtk.Switch(active=reason is None, valign=Gtk.Align.CENTER,
                            sensitive=reason in (None, "blocked"),
                            tooltip_text="Allow this account to run the app")
        switch.connect("state-set", lambda _s, state, r=app["ref"]:
                       (self._set_blocked(user, r, not state), False)[1])
        row.add_suffix(switch)
        remove = Gtk.Button(icon_name="user-trash-symbolic", valign=Gtk.Align.CENTER,
                            tooltip_text="Uninstall for everyone")
        remove.add_css_class("flat")
        remove.connect("clicked", lambda _b, a=app: confirm(
            self.win, f"Uninstall {a['name']}?",
            "Apps are installed for the whole computer, so this removes "
            f"{a['name']} for every user. To keep it but hide it from "
            f"{user['username']}, turn it off instead.", "Uninstall",
            lambda: (self.win.call(lambda: self.win.client.remove_app(a["ref"]),
                                   refresh=False, done_msg=f"Uninstalling {a['name']}…"),
                     GLib.timeout_add_seconds(3, lambda: (self._load_apps(user), False)[1]))))
        row.add_suffix(remove)
        return row

    # -- account -------------------------------------------------------------------

    def _account_tab(self, user: dict) -> list[Adw.PreferencesGroup]:
        group = Adw.PreferencesGroup(title="This account")
        # Hotel and airport Wi-Fi. Without this a filtered laptop cannot
        # reach the sign-in page, so it cannot get online at all — and the
        # only fix was a terminal, which is not a fix.
        captive = Adw.ActionRow(
            title="Allow Wi-Fi sign-in",
            subtitle="Opens this account's connection for 10 minutes so a "
                     "hotel or airport sign-in page can load. Filtering "
                     "resumes on its own.",
            subtitle_lines=3, activatable=True)
        captive.add_suffix(Gtk.Image(icon_name="network-wireless-symbolic"))
        captive.connect("activated", lambda _r: self.win.call(
            lambda: self.win.client.set_captive_mode(user["uid"], 10),
            done_msg=f"{user['username']} can sign in to Wi-Fi for 10 minutes"))
        if user["mode"] == "unfiltered":
            captive.set_sensitive(False)
            captive.set_subtitle("An unfiltered account needs no window.")
        group.add(captive)

        # A child who forgets their password is otherwise locked out for
        # good: there is no root on this machine to reset it with. This
        # hands nobody a password — the login screen asks them for a new
        # one — so the account stays theirs.
        password = Adw.ActionRow(
            title="Reset the password",
            subtitle="They choose a new password the next time they sign in. "
                     "Nothing else about the account changes.",
            subtitle_lines=3, activatable=True)
        password.add_suffix(Gtk.Image(icon_name="dialog-password-symbolic"))
        password.connect("activated", lambda _r: self._confirm_reset_password(user))
        if user.get("admin"):
            password.set_sensitive(False)
            password.set_subtitle("An administrator changes their own password "
                                  "in Settings.")
        group.add(password)

        is_admin = Adw.SwitchRow(
            title="Administrator",
            subtitle="Can change every setting here, including for other people.",
            subtitle_lines=2, active=bool(user.get("admin")))

        def on_admin(switch, _param):
            wanted = switch.get_active()
            if wanted == bool(user.get("admin")):
                return
            self._gated(lambda pw: self.win.client.set_user_admin(user["uid"], wanted, pw),
                        f"{user['username']} is "
                        + ("now an administrator" if wanted else "no longer an administrator"))

        is_admin.connect("notify::active", on_admin)
        group.add(is_admin)

        desktop = Adw.PreferencesGroup(
            title="Desktop",
            description="How this account's desktop looks. Takes effect the "
                        "next time they sign in; filtering is the same "
                        "whichever they use.")
        layout_row = Adw.ComboRow(
            title="Layout",
            model=Gtk.StringList.new([labels.LAYOUT_LABELS[k] for k in labels.LAYOUT_ORDER]))
        current = user.get("layout", "classic")
        layout_row.set_selected(labels.LAYOUT_ORDER.index(current)
                                if current in labels.LAYOUT_ORDER else 0)

        def on_layout(combo, _p):
            new_layout = labels.LAYOUT_ORDER[combo.get_selected()]
            if new_layout == user.get("layout", "classic"):
                return
            self.win.call(
                lambda: self.win.client.set_layout(user["uid"], new_layout),
                done_msg=f"{user['username']} → {labels.LAYOUT_LABELS[new_layout]} "
                         f"at their next sign-in")

        layout_row.connect("notify::selected", on_layout)
        desktop.add(layout_row)
        hint_under(desktop, labels.LAYOUT_HINTS.get(current, ""))

        danger = Adw.PreferencesGroup()
        remove = Adw.ButtonRow(title="Remove This Account…")
        remove.add_css_class("destructive-action")
        remove.connect("activated", lambda *_: confirm_remove_user(self.win, user))
        danger.add(remove)
        return [group, desktop, danger]

    def _confirm_reset_password(self, user: dict):
        return confirm(
            self.win, f"Reset {user['username']}'s password?",
            "The next time they sign in, the login screen asks them to choose "
            "a new password. Nobody sees it, and nothing else about the "
            "account changes.", "Reset",
            lambda: self.win.call(
                lambda: self.win.client.reset_password(user["uid"]),
                done_msg=f"{user['username']} chooses a new password at their "
                         "next sign-in"),
            destructive=False)

    def _guest_account_tab(self, user: dict) -> list[Adw.PreferencesGroup]:
        group = Adw.PreferencesGroup(
            title="Guest account",
            description="Anyone can sign in as the guest without a password. "
                        "Everything the guest does is erased at sign-out. The "
                        "filter settings on the other tabs apply to whoever it is.")
        switch = Adw.SwitchRow(title="Guest account is on", active=True)

        def on_toggle(row, _p):
            if row.get_active():
                return
            self.win.with_guardian(lambda pw: self.win.call(
                lambda: self.win.client.set_guest_config(
                    False, user["mode"], user.get("whitelist", []), pw),
                done_msg="Guest account is off"))
            self.win.go_home()

        switch.connect("notify::active", on_toggle)
        group.add(switch)
        return [group]

    def _confirm_delete_preset(self, key: str) -> None:
        custom = self.win.policy.get("custom_profiles", [])
        label = profiles_mod.get(key, custom).label
        confirm(self.win, f"Delete the {label} group?",
                "The accounts in it keep their settings and are simply in no group "
                "afterwards.",
                "Delete",
                lambda: self._gated(lambda pw: self.win.client.delete_profile(key, pw),
                                    f"Deleted the {label} group"))
