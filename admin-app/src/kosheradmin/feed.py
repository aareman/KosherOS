"""What's happening: the filter's own diary, with people as filters.

A record of the FILTER, not of the person: what it blocked, what it hid,
what it refused to search for, and what the admins changed. Nothing that
was allowed through is ever shown, so this cannot become a browsing
history. Every entry that can be acted on is acted on where it stands — a
blocked page has Allow, and Allow does the same thing approving a request
would have.
"""

from __future__ import annotations

import time

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk  # noqa: E402

from kosherd import activity as activity_mod  # noqa: E402

from . import labels  # noqa: E402
from .common import avatar, clear, error_text, run_async, tag  # noqa: E402
from .dialogs import allow_menu  # noqa: E402

EVERYONE = -1
WEEK = 7 * 86400


class ActivityPage(Gtk.Box):
    """Rail of people on the left, the feed on the right."""

    def __init__(self, win):
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL)
        self.win = win
        self.uid = EVERYONE
        self.since_days = 1
        self.events: list[dict] = []
        self.requests: list[dict] = []

        # -- rail --
        rail = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, width_request=200)
        rail.add_css_class("sidebar-pane" if hasattr(Adw, "NavigationSplitView") else "")
        self.people = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE,
                                  margin_top=8, margin_start=8, margin_end=8)
        self.people.add_css_class("navigation-sidebar")
        self.people.connect("row-selected", self._on_person)
        rail.append(self._rail_label("Show"))
        rail.append(self.people)
        rail.append(Gtk.Separator(margin_top=8, margin_bottom=8))
        self.range = Gtk.Box(spacing=0, margin_start=12, margin_end=12, halign=Gtk.Align.CENTER)
        self.range.add_css_class("linked")
        self.today_btn = Gtk.ToggleButton(label="Today", active=True)
        self.week_btn = Gtk.ToggleButton(label="This week", group=self.today_btn)
        self.today_btn.connect("toggled", lambda b: b.get_active() and self._set_range(1))
        self.week_btn.connect("toggled", lambda b: b.get_active() and self._set_range(7))
        self.range.append(self.today_btn)
        self.range.append(self.week_btn)
        rail.append(self.range)
        self.append(rail)
        self.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))

        # -- feed --
        self.feed = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18,
                            margin_top=18, margin_bottom=24, margin_start=12,
                            margin_end=12)
        clamp = Adw.Clamp(maximum_size=760, tightening_threshold=500)
        clamp.set_child(self.feed)
        scroller = Gtk.ScrolledWindow(hexpand=True, vexpand=True,
                                      hscrollbar_policy=Gtk.PolicyType.NEVER)
        scroller.set_child(clamp)
        self.append(scroller)
        self._rebuild_rail()

    @staticmethod
    def _rail_label(text: str) -> Gtk.Label:
        label = Gtk.Label(label=text.upper(), xalign=0, margin_start=20,
                          margin_top=14, margin_bottom=2)
        label.add_css_class("caption-heading")
        label.add_css_class("dim-label")
        return label

    # -- state ------------------------------------------------------------------

    def show_user(self, uid: int) -> None:
        """Narrow the feed to one person (from a card's "See all")."""
        self.uid = uid
        row = self.people.get_first_child()
        while row is not None:
            if getattr(row, "uid", None) == uid:
                self.people.select_row(row)
                break
            row = row.get_next_sibling()
        self.refresh()

    def _on_person(self, _list, row) -> None:
        if row is None:
            return
        uid = getattr(row, "uid", EVERYONE)
        if uid != self.uid:
            self.uid = uid
            self.refresh()

    def _set_range(self, days: int) -> None:
        if days != self.since_days:
            self.since_days = days
            self.refresh()

    def refresh(self) -> None:
        self._rebuild_rail()
        since = activity_mod.day_start() if self.since_days == 1 \
            else int(time.time()) - WEEK

        def load():
            return (self.win.client.list_activity(since, self.uid),
                    self.win.client.list_requests())

        def on_done(result):
            self.events, self.requests = result
            self._render()

        run_async(load, on_done, lambda e: self.win.toast(error_text(e)))

    # -- rail -------------------------------------------------------------------

    def _rebuild_rail(self) -> None:
        selected = self.uid
        waiting: dict[int, int] = {}
        for request in self.requests:
            waiting[request["uid"]] = waiting.get(request["uid"], 0) + 1
        clear(self.people)
        everyone = self._person_row("Everyone", EVERYONE, sum(waiting.values()),
                                    icon="system-users-symbolic")
        self.people.append(everyone)
        to_select = everyone
        for user in self.win.policy.get("users", []):
            row = self._person_row(user["username"], user["uid"],
                                   waiting.get(user["uid"], 0))
            self.people.append(row)
            if user["uid"] == selected:
                to_select = row
        self.people.select_row(to_select)

    @staticmethod
    def _person_row(name: str, uid: int, waiting: int, icon: str | None = None) -> Gtk.ListBoxRow:
        row = Gtk.ListBoxRow()
        row.uid = uid
        box = Gtk.Box(spacing=8, margin_top=4, margin_bottom=4)
        if icon:
            box.append(Gtk.Image(icon_name=icon, pixel_size=16, margin_start=4, margin_end=4))
        else:
            box.append(avatar(name, 24))
        label = Gtk.Label(label=name, xalign=0, hexpand=True, ellipsize=3)
        box.append(label)
        if waiting:
            box.append(tag(str(waiting), "acc"))
        row.set_child(box)
        return row

    # -- feed -------------------------------------------------------------------

    def _render(self) -> None:
        clear(self.feed)
        shown_requests = [r for r in self.requests
                          if self.uid == EVERYONE or r["uid"] == self.uid]
        if shown_requests:
            from .dialogs import request_row

            group = Adw.PreferencesGroup(title=f"Waiting for you ({len(shown_requests)})")
            for request in shown_requests:
                group.add(request_row(self.win, request,
                                      on_answered=lambda _r: self.win.reload()))
            self.feed.append(group)

        folded = fold(self.events)
        if not folded:
            page = Adw.StatusPage(
                icon_name="emblem-ok-symbolic",
                title="Nothing to show" if self.since_days == 1 else "A quiet week",
                description=("The filter has not blocked anything today"
                             + ("" if self.uid == EVERYONE else " for this account")
                             + ". What was allowed through is never recorded."),
                vexpand=True)
            page.add_css_class("compact")
            self.feed.append(page)
            return

        current_day = None
        group = None
        for event in folded:
            day = labels.day_label(event["t"])
            if day != current_day:
                current_day = day
                group = Adw.PreferencesGroup(title=day)
                self.feed.append(group)
            group.add(self._event_row(event))

    def _event_row(self, event: dict) -> Adw.ActionRow:
        kind = event["kind"]
        name = event.get("username", "?")
        url = event.get("url", "")
        why = labels.why_text(event.get("why", ""))
        times = event.get("times", 1)
        again = f" · {times} times" if times > 1 else ""
        row = Adw.ActionRow(subtitle_lines=2, use_markup=False)
        clock = Gtk.Label(label=time.strftime("%H:%M", time.localtime(event["t"])),
                          valign=Gtk.Align.CENTER)
        clock.add_css_class("dim-label")
        clock.add_css_class("caption")
        clock.add_css_class("time-label")
        row.add_prefix(clock)

        if kind == activity_mod.CHANGE:
            what, detail = labels.change_sentence(event, self.win.policy)
            row.set_title(what)
            row.set_subtitle(detail)
            row.add_prefix(avatar(event.get("by_username") or "?", 24))
            return row

        row.add_prefix(avatar(name, 24))
        if kind == activity_mod.BLOCK:
            row.set_title(f"Blocked {labels.short_url(url)}")
            row.set_subtitle(" · ".join(p for p in (name, why, again.strip(" ·")) if p))
            if self._allowable(event):
                row.add_suffix(allow_menu(self.win, event["uid"], name, url,
                                          on_done=self.refresh))
        elif kind == activity_mod.PICTURES:
            row.set_title(f"Hid pictures on {labels.short_url(url)}")
            row.set_subtitle(f"{name} · the page was shown, its pictures were covered{again}")
        elif kind == activity_mod.VIDEO:
            row.set_title(f"Refused a video on {labels.short_url(url) or 'a page'}")
            row.set_subtitle(f"{name} · after looking at it{again}")
        elif kind == activity_mod.SEARCH:
            row.set_title(f"Would not search for “{event.get('text', '')}”")
            row.set_subtitle(" · ".join(p for p in (name, event.get("why", ""), again.strip(" ·")) if p))
        return row

    def _allowable(self, event: dict) -> bool:
        """Allow makes sense for a block the filter decided; not for one an
        admin wrote as a rule, and not for an account that is gone."""
        if event.get("username", "?") == "?":
            return False
        return not (event.get("why") or "").startswith("rule:")


def fold(events: list[dict]) -> list[dict]:
    """Collapse repeats: the same person hitting the same page five times
    is one line saying so, not five lines."""
    out: list[dict] = []
    seen: dict[tuple, dict] = {}
    for event in events:
        key = (event["uid"], event["kind"], event.get("url", ""),
               event.get("text", ""), labels.day_label(event["t"]))
        if event["kind"] != activity_mod.CHANGE and key in seen:
            seen[key]["times"] = seen[key].get("times", 1) + 1
            continue
        entry = dict(event)
        seen[key] = entry
        out.append(entry)
    return out
