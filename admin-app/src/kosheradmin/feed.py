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

from . import labels, seen  # noqa: E402
from .common import avatar, clear, error_text, pointer_cursors, run_async, tag  # noqa: E402
from .dialogs import allow_menu, pictures_menu  # noqa: E402

EVERYONE = -1
WEEK = 7 * 86400


class ActivityPage(Gtk.Box):
    """The feed, the full width of the page.

    Who and when are chosen from the header bar rather than from a rail
    down the left: the app already has a sidebar there, and two navigation
    columns for one page is one too many.
    """

    def __init__(self, win):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.win = win
        self.uid = EVERYONE
        self.since_days = 1
        self.events: list[dict] = []
        self.requests: list[dict] = []
        # When this administrator had last looked, as this visit began:
        # what is newer is marked New for the whole visit. None until the
        # page has been opened.
        self.visit_since: int | None = None

        # -- the filters, which the window hands to the header bar --
        self.people = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE,
                                  margin_top=4, margin_bottom=4)
        self.people.add_css_class("navigation-sidebar")
        self.people.connect("row-selected", self._on_person)
        people_scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER,
                                             propagate_natural_height=True,
                                             max_content_height=420,
                                             width_request=220)
        people_scroller.set_child(self.people)
        self.who = Gtk.MenuButton(label="Everyone", always_show_arrow=True,
                                  popover=Gtk.Popover(child=people_scroller))
        self.who.add_css_class("flat")

        self.range = Gtk.Box(spacing=0)
        self.range.add_css_class("linked")
        self.today_btn = Gtk.ToggleButton(label="Today", active=True)
        self.week_btn = Gtk.ToggleButton(label="This week", group=self.today_btn)
        self.today_btn.connect("toggled", lambda b: b.get_active() and self._set_range(1))
        self.week_btn.connect("toggled", lambda b: b.get_active() and self._set_range(7))
        self.range.append(self.today_btn)
        self.range.append(self.week_btn)

        # -- feed --
        self.feed = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18,
                            margin_top=18, margin_bottom=24, margin_start=12,
                            margin_end=12)
        clamp = Adw.Clamp(maximum_size=820, tightening_threshold=560)
        clamp.set_child(self.feed)
        scroller = Gtk.ScrolledWindow(hexpand=True, vexpand=True,
                                      hscrollbar_policy=Gtk.PolicyType.NEVER)
        scroller.set_child(clamp)
        self.append(scroller)
        self._rebuild_rail()

    # -- state ------------------------------------------------------------------

    def begin_visit(self) -> None:
        """The administrator opened Activity: what came since their last
        look is marked New while they are here, and the count beside
        Activity clears."""
        self.visit_since = seen.load()
        self._looked()

    def _looked(self) -> None:
        seen.save()
        self.win.activity_unseen = 0
        sidebar = getattr(self.win, "sidebar", None)
        if sidebar is not None:
            sidebar.say_activity()

    def _on_screen(self) -> bool:
        win = self.win
        return (getattr(win, "destination", None) == "activity"
                and bool(win.get_property("is-active")))

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
        self.who.set_label(getattr(row, "person", "Everyone"))
        popover = self.who.get_popover()
        if popover is not None:
            popover.popdown()
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
            if self.visit_since is not None and self._on_screen():
                # Whatever arrived while the page is open has been seen.
                self._looked()

        run_async(load, on_done, lambda e: self.win.toast(error_text(e)))

    # -- who the feed is about ---------------------------------------------------

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
        row.person = name
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
        fresh: dict[Adw.PreferencesGroup, int] = {}
        for event in folded:
            day = labels.day_label(event["t"])
            if day != current_day:
                current_day = day
                group = Adw.PreferencesGroup(title=day)
                self.feed.append(group)
            row = self._event_row(event)
            row.is_new = self._is_new(event)
            if row.is_new:
                # Beside the title, before the actions: read first.
                row.add_prefix(new_tag())
                fresh[group] = fresh.get(group, 0) + 1
            group.add(row)
        for day_group, count in fresh.items():
            day_group.set_description(new_since_sentence(count, self.visit_since))
        pointer_cursors(self)

    def _is_new(self, event: dict) -> bool:
        return (self.visit_since is not None and seen.is_news(event)
                and event["t"] > self.visit_since)

    def _event_row(self, event: dict) -> Adw.ActionRow:
        kind = event["kind"]
        name = event.get("username", "?")
        times = event.get("times", 1)
        again = f"{times} times" if times > 1 else ""

        if kind in (activity_mod.CHANGE, activity_mod.TIME):
            row = Adw.ActionRow(subtitle_lines=2, use_markup=False)
            row.add_prefix(self._clock(event))
            if kind == activity_mod.CHANGE:
                what, detail = labels.change_sentence(event, self.win.policy)
                row.set_title(what)
                row.set_subtitle(detail)
                row.add_prefix(avatar(event.get("by_username") or "?", 24))
            else:
                row.add_prefix(avatar(name, 24))
                row.set_title(labels.time_event_title(event))
                row.set_subtitle(" · ".join(p for p in (name, again) if p))
            return row

        # What was blocked and what kind of thing it was, with the details
        # an admin needs to decide behind the row's arrow.
        what = blocked_kind(event)
        word = KIND_INFO[what][0]
        url = event.get("url", "")
        row = Adw.ExpanderRow(title_lines=1, subtitle_lines=1, use_markup=False)
        row.add_prefix(self._clock(event))
        row.add_prefix(kind_badge(what))
        if kind == activity_mod.SEARCH:
            row.set_title(f"Blocked search: “{event.get('text', '')}”")
            why = event.get("why", "")
        elif kind == activity_mod.PICTURES:
            row.set_title(f"Hid pictures on {labels.short_url(url)}")
            why = "Page shown with pictures covered"
        elif kind == activity_mod.VIDEO:
            row.set_title(f"Blocked video on {labels.short_url(url) or 'a page'}")
            why = "Blocked by video filtering"
        else:
            row.set_title(f"Blocked {labels.short_url(url)}")
            why = labels.why_text(event.get("why", ""), event.get("source", ""))
        row.set_subtitle(" · ".join(p for p in (word, name, why, again) if p))

        if kind == activity_mod.BLOCK and self._allowable(event):
            row.add_suffix(allow_menu(self.win, event["uid"], name, url,
                                      on_done=self.refresh))
        elif kind == activity_mod.PICTURES and name != "?" and url:
            row.add_suffix(pictures_menu(self.win, event["uid"], name, url,
                                         on_done=self.refresh))
        for title, value in details(event, what, why):
            # Markup off before the text goes in: an address with "&" in
            # it is not markup, and would otherwise show as nothing.
            detail = Adw.ActionRow(use_markup=False, subtitle_lines=0,
                                   subtitle_selectable=True)
            detail.set_title(title)
            detail.set_subtitle(value)
            detail.add_css_class("property")
            row.add_row(detail)
        return row

    @staticmethod
    def _clock(event: dict) -> Gtk.Label:
        clock = Gtk.Label(label=time.strftime("%H:%M", time.localtime(event["t"])),
                          valign=Gtk.Align.CENTER)
        clock.add_css_class("dim-label")
        clock.add_css_class("caption")
        clock.add_css_class("time-label")
        return clock

    def _allowable(self, event: dict) -> bool:
        """Allow makes sense for a block the filter decided; not for one an
        admin wrote as a rule, and not for an account that is gone."""
        if event.get("username", "?") == "?":
            return False
        return not (event.get("why") or "").startswith("rule:")


def new_tag() -> Gtk.Label:
    """The word New on a line that came since the last look. A word, so
    colour is never the only way to tell."""
    label = tag("New", "acc")
    label.update_property([Gtk.AccessibleProperty.LABEL], ["New since you last looked"])
    return label


def new_since_sentence(count: int, last: int | None, now: float | None = None) -> str:
    """Under a day's heading: how many of its lines are new, and since when."""
    what = f"{count} new"
    if not last:
        return what
    clock = time.strftime("%H:%M", time.localtime(last))
    day = labels.day_label(last, now)
    if day == "Today":
        return f"{what} since you last looked, at {clock}"
    if day == "Yesterday":
        return f"{what} since you last looked, yesterday at {clock}"
    return f"{what} since you last looked, on {day} at {clock}"


def fold(events: list[dict]) -> list[dict]:
    """Collapse repeats: the same person hitting the same page five times
    is one line saying so, not five lines."""
    out: list[dict] = []
    seen: dict[tuple, dict] = {}
    for event in events:
        # A time event has no page; what distinguishes a sign-out from a
        # refused sign-in is the reason.
        why = event.get("why", "") if event["kind"] == activity_mod.TIME else ""
        key = (event["uid"], event["kind"], event.get("url", ""),
               event.get("text", ""), why, labels.day_label(event["t"]))
        if event["kind"] != activity_mod.CHANGE and key in seen:
            seen[key]["times"] = seen[key].get("times", 1) + 1
            # Newest first, so each repeat is earlier than the last.
            seen[key]["first"] = event["t"]
            continue
        entry = dict(event)
        seen[key] = entry
        out.append(entry)
    return out


# -- what was blocked -----------------------------------------------------------

# kind: (the word in the row, icon, colour class, what it was in a sentence)
KIND_INFO = {
    "page": ("Page", "web-browser-symbolic", "err", "A page"),
    "picture": ("Picture", "image-x-generic-symbolic", "warn", "A picture"),
    "video": ("Video", "video-x-generic-symbolic", "acc", "A video"),
    "search": ("Search", "system-search-symbolic", "plain", "A search"),
    "part": ("Part of a page", "text-x-generic-symbolic", "plain", "Part of a page"),
}

# The browser's Sec-Fetch-Dest for the things a page loads besides itself.
PART_WORDS = {"script": "a script", "style": "a style sheet", "font": "a font",
              "manifest": "app information", "worker": "a background script",
              "sharedworker": "a background script",
              "serviceworker": "a background script",
              "empty": "data the page asked for"}

PICTURE_ENDINGS = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif", ".svg",
                   ".bmp", ".ico")
VIDEO_ENDINGS = (".mp4", ".webm", ".m3u8", ".mpd", ".mov", ".m4v")


def blocked_kind(event: dict) -> str:
    """page, picture, video, search or part, from what the record says.

    A block written before the filter noted what the browser was fetching
    goes by the end of its address, and is a page otherwise."""
    kind = event.get("kind")
    if kind == activity_mod.PICTURES:
        return "picture"
    if kind == activity_mod.VIDEO or (event.get("why") or "").startswith("youtube:"):
        return "video"
    if kind == activity_mod.SEARCH:
        return "search"
    dest = (event.get("dest") or "").lower()
    if dest in ("document", "iframe", "frame", "embed", "object"):
        return "page"
    if dest == "image":
        return "picture"
    if dest in ("video", "audio", "track"):
        return "video"
    if dest:
        return "part"
    from urllib.parse import urlsplit

    try:
        path = urlsplit(event.get("url", "")).path.lower()
    except ValueError:
        path = ""
    if path.endswith(PICTURE_ENDINGS):
        return "picture"
    if path.endswith(VIDEO_ENDINGS):
        return "video"
    return "page"


def kind_badge(what: str) -> Gtk.Widget:
    """A round, coloured icon for the kind. The row's subtitle names the
    kind in words too, so colour is never the only way to tell."""
    word, icon, colour, _sentence = KIND_INFO[what]
    badge = Gtk.Image(icon_name=icon, pixel_size=16, valign=Gtk.Align.CENTER,
                      halign=Gtk.Align.CENTER,
                      tooltip_text=f"Blocked: {word.lower()}")
    badge.set_size_request(32, 32)
    badge.add_css_class("kind-badge")
    badge.add_css_class(colour)
    badge.update_property([Gtk.AccessibleProperty.LABEL], [f"Blocked {word.lower()}"])
    badge.kind = what
    return badge


def details(event: dict, what: str, why: str) -> list[tuple[str, str]]:
    """The lines under an opened row: what the record knows that helps
    decide whether to allow it."""
    out: list[tuple[str, str]] = []
    kind = event["kind"]
    url = event.get("url", "")
    if kind == activity_mod.SEARCH:
        if event.get("text"):
            out.append(("Searched for", event["text"]))
    elif kind in (activity_mod.PICTURES, activity_mod.VIDEO):
        if url:
            out.append(("On the page", url))
    else:
        if url:
            out.append(("Address", url))
        if event.get("page") and event["page"] != url:
            out.append(("On the page", event["page"]))
    sentence = KIND_INFO[what][3]
    dest = (event.get("dest") or "").lower()
    if what == "part" and dest in PART_WORDS:
        sentence = f"Part of a page: {PART_WORDS[dest]}"
    out.append(("What it was", sentence))
    if why:
        out.append(("Why", why[:1].upper() + why[1:]))
    out.append(("Account", event.get("username", "?")))
    when = time.strftime("%A %-d %B, %H:%M", time.localtime(event["t"]))
    times = event.get("times", 1)
    if times > 1 and event.get("first"):
        first = time.strftime("%H:%M", time.localtime(event["first"]))
        when += f" ({times} times since {first})"
    out.append(("When", when))
    return out
