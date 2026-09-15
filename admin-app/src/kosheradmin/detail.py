"""One person, one full page.

The top answers "how is Yosef protected?" before any setting is touched:
the preset and how far the account has drifted from it, and the protection
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
from kosherd import profiles as profiles_mod  # noqa: E402
from kosherd.policy import MEDIA_LEVELS, MODES, YOUTUBE_CATEGORIES  # noqa: E402

from . import labels  # noqa: E402
from .common import (avatar, chip, clear, confirm, error_text, mode_badge,  # noqa: E402
                     pointer_cursors, run_async, small_button, submit_on_enter, tag)
from .dialogs import (RulesDialog, SavePresetDialog, WhitelistDialog, allow_menu,  # noqa: E402
                      confirm_remove_user, request_row)
from .feed import fold  # noqa: E402

TABS = (("overview", "Overview", "view-list-symbolic"),
        ("filtering", "Filtering", "web-browser-symbolic"),
        ("media", "Pictures & words", "image-x-generic-symbolic"),
        ("youtube", "YouTube", "video-display-symbolic"),
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


def every_preset_blocks() -> set[str]:
    """Categories every built-in preset that blocks anything blocks. Labelled
    on the grid so a parent knows those are not the ones to think about."""
    sets = [set(p.blocked_categories) for p in profiles_mod.PROFILES
            if p.blocked_categories]
    return set.intersection(*sets) if sets else set()


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
        switcher = Adw.ViewSwitcher(stack=self.stack, policy=Adw.ViewSwitcherPolicy.WIDE,
                                    halign=Gtk.Align.CENTER, margin_top=6, margin_bottom=6)
        view = Adw.ToolbarView()
        view.add_top_bar(header)
        view.add_top_bar(switcher)
        view.set_content(self.stack)
        self.set_child(view)
        pointer_cursors(self)

    def _gated(self, work, done_msg: str) -> None:
        self.win.with_guardian(lambda pw: self.win.call(lambda: work(pw), done_msg=done_msg))

    # -- overview ------------------------------------------------------------------

    def _overview_tab(self, user: dict) -> list[Adw.PreferencesGroup]:
        return [self._identity_group(user), *self._today_groups(user)]

    def _identity_group(self, user: dict) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup()
        custom = self.win.policy.get("custom_profiles", [])
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)

        who = Gtk.Box(spacing=14)
        who.append(avatar(user["username"], 56))
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
        setup = Gtk.Label(label="Set up as " + labels.setup_sentence(
            self.preset_key, self.drift, custom), xalign=0, wrap=True)
        setup.add_css_class("dim-label")
        names.append(setup)
        who.append(names)
        actions = Gtk.Box(spacing=6, halign=Gtk.Align.END, hexpand=True,
                          valign=Gtk.Align.CENTER)
        if self.preset_key and labels.drift_sentences(self.drift):
            label = profiles_mod.get(self.preset_key, custom).label
            reset = small_button(f"Reset to {label}")
            reset.connect("clicked", lambda _b: self._gated(
                lambda pw: self.win.client.apply_profile(user["uid"], self.preset_key, pw),
                f"{user['username']} → {label}"))
            actions.append(reset)
        save = small_button("Save as a preset…")
        save.connect("clicked", lambda _b: SavePresetDialog(self.win, user).present(self.win))
        actions.append(save)
        who.append(actions)
        box.append(who)

        chips = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, column_spacing=6,
                            row_spacing=6, max_children_per_line=8, homogeneous=False)
        for icon, text, protects in labels.protection_lines(user):
            chips.append(chip(text, "blocking" if protects else "open", icon=icon))
        for sentence in labels.drift_sentences(self.drift):
            chips.append(chip(sentence, "diff"))
        box.append(chips)
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
        return row

    # -- filtering -----------------------------------------------------------------

    def _filtering_tab(self, user: dict) -> list[Adw.PreferencesGroup]:
        # A whitelist account reaches its approved list and nothing else, so
        # "which kinds of site are blocked" is a question with no meaning
        # there — fifteen toggles that change nothing. The approved list
        # takes their place.
        if user["mode"] == "whitelist":
            return [self._mode_group(user), self._approved_sites_group(user),
                    self._pages_group(user)]
        return [self._mode_group(user), self._categories_group(user),
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
        profiles = profiles_mod.all_profiles(custom)
        keys = [p.key for p in profiles]
        current = profiles_mod.matching(user, custom)
        profile_row = Adw.ComboRow(
            title="Set up as",
            model=Gtk.StringList.new(
                [p.label + (" (yours)" if p.key.startswith(profiles_mod.CUSTOM_PREFIX) else "")
                 for p in profiles] + [labels.PROFILE_CUSTOM]))
        profile_row.set_selected(keys.index(current) if current else len(keys))
        profile_hint = (profiles_mod.get(current, custom).description if current
                        else "These settings do not match any preset exactly; the "
                             "changes are listed on the Overview tab.")

        def on_profile(combo, _p):
            index = combo.get_selected()
            if index >= len(keys):
                return  # "Custom" is a readout, not a thing you can pick
            key = keys[index]
            if key == current:
                return
            self._gated(lambda pw: self.win.client.apply_profile(user["uid"], key, pw),
                        f"{user['username']} → {profiles_mod.get(key, custom).label}")

        profile_row.connect("notify::selected", on_profile)
        group.add(profile_row)

        if current and current.startswith(profiles_mod.CUSTOM_PREFIX):
            delete_row = Adw.ButtonRow(title="Delete This Preset…")
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
            preset_btn = small_button(f"{label} preset")
            preset_btn.set_tooltip_text(f"Block what the {label} preset blocks")
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
        floor = every_preset_blocks()
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
                    line.append(tag("every preset"))
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
            settings["allowed_channels"] = self.yt_channels
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
        dialog = Adw.AlertDialog(
            heading="Approve a channel",
            body="Paste the channel handle (@example) or its ID (UC…). It is "
                 "in the address of any of the channel's videos.")
        entry = Gtk.Entry(placeholder_text="@example")
        dialog.set_extra_child(entry)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("add", "Add")
        dialog.set_response_appearance("add", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("add")
        dialog.set_close_response("cancel")
        submit_on_enter(dialog, "add", entry)

        def on_response(_d, response):
            value = entry.get_text().strip()
            if response == "add" and value and value not in self.yt_channels:
                self.yt_channels.append(value)
                self._rebuild_channels()
                self._save_youtube()

        dialog.connect("response", on_response)
        dialog.present(self.win)

    def _rebuild_channels(self) -> None:
        clear(self.channel_list)
        if not self.yt_channels:
            self.channel_list.append(Adw.ActionRow(
                title="Every channel is allowed",
                subtitle="Only the settings below apply."))
            return
        for name in self.yt_channels:
            row = Adw.ActionRow(title=name, use_markup=False)
            remove = Gtk.Button(icon_name="user-trash-symbolic", valign=Gtk.Align.CENTER)
            remove.add_css_class("flat")
            remove.connect("clicked", lambda _b, n=name: (
                self.yt_channels.remove(n), self._rebuild_channels(), self._save_youtube()))
            row.add_suffix(remove)
            self.channel_list.append(row)

    # -- apps ----------------------------------------------------------------------

    def _apps_tab(self, user: dict) -> list[Adw.PreferencesGroup]:
        """Apps on this computer, from one user's point of view.

        Apps install system-wide, so 'uninstall' removes an app for
        everyone — that is spelled out. To restrict a single user, turn
        the app off for them instead (enforced by malcontent).
        """
        installs_group = Adw.PreferencesGroup()
        installs = Adw.SwitchRow(title="Can install approved apps",
                                 subtitle="Only apps on the approved list, from the KosherOS Store",
                                 active=user.get("can_install_apps", True))
        installs.connect("notify::active", lambda s, _p: (
            s.get_active() != user.get("can_install_apps", True) and self.win.call(
                lambda: self.win.client.set_user_can_install(user["uid"], s.get_active()),
                done_msg=f"App installs {'enabled' if s.get_active() else 'disabled'} for {user['username']}")))
        installs_group.add(installs)

        self.apps_group = Adw.PreferencesGroup(
            title="Installed apps",
            description="Turning an app off hides it from this user only. "
                        "Uninstalling removes it from the whole computer.")
        self.apps_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.apps_list.add_css_class("boxed-list")
        self.apps_list.append(Adw.ActionRow(title="Loading…"))
        self.apps_group.add(self.apps_list)
        # An empty allow-list means "every installed app".
        self.allowed: set[str] | None = set(user.get("apps") or []) or None
        self._load_apps(user)
        return [installs_group, self.apps_group]

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
                apps_list.append(self._app_row(user, app, details))
            pointer_cursors(apps_list)

        run_async(self.win.client.list_installed_details, on_done,
                  lambda e: self.win.toast(error_text(e)))

    def _app_row(self, user: dict, app: dict, all_apps: list[dict]) -> Adw.ActionRow:
        subtitle = app["ref"]
        if app.get("installed_by"):
            subtitle += f" · installed by {app['installed_by']}"
        if not app.get("approved", True):
            subtitle += " · no longer approved"
        row = Adw.ActionRow(title=app["name"], subtitle=subtitle, use_markup=False)
        allowed = self.allowed is None or app["ref"] in self.allowed
        switch = Gtk.Switch(active=allowed, valign=Gtk.Align.CENTER,
                            tooltip_text="Allow this user to run the app")
        switch.connect("state-set", lambda _s, state, r=app["ref"], every=all_apps:
                       self._set_allowed(user, r, state, every))
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

    def _set_allowed(self, user: dict, ref: str, allowed: bool, all_apps: list[dict]) -> bool:
        if self.allowed is None:
            # Was "everything"; materialise the list so one app can be dropped.
            self.allowed = {a["ref"] for a in all_apps}
        if allowed:
            self.allowed.add(ref)
        else:
            self.allowed.discard(ref)
        refs = [] if self.allowed == {a["ref"] for a in all_apps} else sorted(self.allowed)
        self.win.call(lambda: self.win.client.set_user_apps(user["uid"], refs),
                      refresh=False,
                      done_msg=f"{'Allowed' if allowed else 'Blocked'} for {user['username']}")
        return False

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
            self.win.nav.pop_to_tag("root")

        switch.connect("notify::active", on_toggle)
        group.add(switch)
        return [group]

    def _confirm_delete_preset(self, key: str) -> None:
        custom = self.win.policy.get("custom_profiles", [])
        label = profiles_mod.get(key, custom).label
        confirm(self.win, f"Delete the preset “{label}”?",
                "Accounts set up with it keep their settings; only the preset itself goes.",
                "Delete",
                lambda: self._gated(lambda pw: self.win.client.delete_profile(key, pw),
                                    f"Deleted preset “{label}”"))
