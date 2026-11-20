"""Usage: how much went through the filter and how much it stopped.

Numbers, and charts of them, for today, the past week, the past month and
all time, for everyone or one person: pages and files loaded, data in and
out, what was blocked, and how long the filter took over each request.
The week and the month show each day as a bar (charts.py); every number
is still written out in the rows under its chart. Like the activity feed
it never names a site, so it cannot become a browsing history; the
activity feed is where a block can be looked at and undone.

When and who are chosen in the header bar, as on the Activity page.
"""

from __future__ import annotations

import datetime

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

from kosherd import usage as usage_mod  # noqa: E402

from . import charts, labels  # noqa: E402
from .common import avatar, banner_slot, clear, error_text, pointer_cursors, run_async  # noqa: E402

EVERYONE = -1
RANGES = (("today", "Today"), ("week", "This week"), ("month", "This month"),
          ("all", "All time"))


def _bound(ms: int, *, unit: bool = False) -> str:
    """A filter-time bucket under its bar: 1 ms, 2, 5 … 500, 1 s, 2.5, 5."""
    if ms >= 1000:
        text = f"{ms / 1000:g}"
        return text + " s" if unit else text
    return f"{ms} ms" if unit else str(ms)


def count(n: int | None) -> str:
    return f"{n or 0:,}"


def size(n: int | None) -> str:
    return GLib.format_size(n or 0)


def millis(ms: int | float | None) -> str:
    if ms is None:
        return "—"
    if ms >= usage_mod.SLOWEST_MS:
        return f"over {usage_mod.LATENCY_BOUNDS_MS[-1] // 1000} s"
    if ms >= 1000:
        return f"{ms / 1000:g} s"
    return f"{ms:g} ms"


def counted(user: dict) -> bool:
    """Whether this account's web traffic goes through the filter at all.
    Only filtered internet does; the other settings decide by address and
    never hand a page to the filter to count."""
    from kosherd.policy import INSPECTED_MODES

    return user.get("mode") in INSPECTED_MODES


def not_counted_why(user: dict) -> str:
    mode = user.get("mode")
    if mode == "none":
        return "This account has no internet access."
    if mode == "unfiltered":
        return ("Content filtering is off for this account, so its pages do not "
                "pass through the filter to be counted.")
    return (f"On {labels.MODE_LABELS.get(mode, mode)}, sites are allowed or blocked "
            "by name before a page loads, so the filter never sees the pages and "
            "there is nothing to count. Usage is counted on Filtered internet.")


class UsagePage(Adw.NavigationPage):
    def __init__(self, win):
        super().__init__(title="Usage", tag="usage")
        self.win = win
        self.range = getattr(win, "usage_range", "today")
        self.uid = getattr(win, "usage_uid", EVERYONE)
        self.data: dict | None = None

        # -- header: when, and who --
        self.ranges = Gtk.Box()
        self.ranges.add_css_class("linked")
        first = None
        for key, title in RANGES:
            button = Gtk.ToggleButton(label=title, active=key == self.range,
                                      group=first)
            button.connect("toggled", lambda b, k=key: b.get_active() and self._set_range(k))
            first = first or button
            self.ranges.append(button)

        self.people = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE,
                                  margin_top=4, margin_bottom=4)
        self.people.add_css_class("navigation-sidebar")
        self.people.connect("row-activated", lambda _l, row: self.show_user(row.uid))
        scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER,
                                      propagate_natural_height=True,
                                      max_content_height=420, width_request=220)
        scroller.set_child(self.people)
        self.who = Gtk.MenuButton(always_show_arrow=True,
                                  popover=Gtk.Popover(child=scroller))
        self.who.add_css_class("flat")

        header = Adw.HeaderBar(title_widget=self.ranges)
        header.pack_start(self.who)
        self.banner_slot = banner_slot()
        self.prefs = Adw.PreferencesPage()
        view = Adw.ToolbarView()
        view.add_top_bar(header)
        view.add_top_bar(self.banner_slot)
        view.set_content(self.prefs)
        self.set_child(view)

        self._fill_people()
        self._loading()
        self.refresh()

    # -- state ------------------------------------------------------------------

    def _users(self) -> list[dict]:
        from .family import guest_user

        users = list(self.win.policy.get("users", []))
        guest = guest_user(self.win.policy)
        if guest is not None:
            users.append(guest)
        return users

    def _name(self, uid: int) -> str:
        if uid == EVERYONE:
            return "Everyone"
        user = next((u for u in self._users() if u["uid"] == uid), None)
        if user is None:
            return "Removed account"
        return "Guest" if user.get("guest") else user["username"]

    def show_user(self, uid: int) -> None:
        self.uid = self.win.usage_uid = uid
        self.who.get_popover().popdown()
        self._fill_people()
        self._render()

    def _set_range(self, key: str) -> None:
        if key != self.range:
            self.range = self.win.usage_range = key
            self._render()

    def refresh(self) -> None:
        def on_done(data):
            self.data = data
            self._render()

        run_async(self.win.client.usage, on_done,
                  lambda e: self.win.toast(error_text(e)))

    def _fill_people(self) -> None:
        self.who.set_label(self._name(self.uid))
        clear(self.people)
        for uid, name in ((EVERYONE, "Everyone"),
                          *((u["uid"], self._name(u["uid"])) for u in self._users())):
            row = Gtk.ListBoxRow()
            row.uid = uid
            box = Gtk.Box(spacing=10, margin_top=6, margin_bottom=6,
                          margin_start=6, margin_end=6)
            if uid == EVERYONE:
                box.append(Gtk.Image(icon_name="system-users-symbolic", pixel_size=24))
            else:
                box.append(avatar(name, 24))
            box.append(Gtk.Label(label=name, xalign=0, hexpand=True))
            if uid == self.uid:
                box.append(Gtk.Image(icon_name="object-select-symbolic"))
            row.set_child(box)
            self.people.append(row)

    # -- the page -----------------------------------------------------------------

    def _loading(self) -> None:
        self._clear()
        group = Adw.PreferencesGroup()
        group.add(Adw.ActionRow(title="Loading…"))
        self._add(group)

    def _clear(self) -> None:
        for group in getattr(self, "_groups", []):
            self.prefs.remove(group)
        self._groups = []

    def _add(self, group: Adw.PreferencesGroup) -> None:
        self.prefs.add(group)
        self._groups.append(group)

    def _render(self) -> None:
        if self.data is None:
            return
        self._clear()
        ranged = self.data.get(self.range) or {}
        if self.uid == EVERYONE:
            numbers = ranged.get("everyone") or {}
        else:
            numbers = (ranged.get("people") or {}).get(str(self.uid)) or {}
            user = next((u for u in self._users() if u["uid"] == self.uid), None)
            if user is not None and not counted(user) and not numbers.get("requests"):
                self._not_counted(user)
                return

        days, series = self._series(ranged)

        traffic = Adw.PreferencesGroup(title="Internet")
        if days:
            self._lead(traffic, self._day_chart(
                "Pages and files loaded each day", days,
                [point.get("requests", 0) for point in series],
                lambda n: f"{count(n)} loaded"))
        self._number(traffic, "Pages and files loaded", count(numbers.get("requests")))
        self._number(traffic, "Downloaded", size(numbers.get("bytes_in")))
        self._number(traffic, "Uploaded", size(numbers.get("bytes_out")))
        self._add(traffic)

        stopped = Adw.PreferencesGroup(title="Blocked")
        if days:
            self._lead(stopped, self._day_chart(
                "Pages blocked each day", days,
                [point.get("blocked", 0) for point in series],
                lambda n: f"{count(n)} blocked", red=True, empty="Nothing blocked"))
        kinds = (("Pages", "blocked"), ("Pictures", "pictures"),
                 ("Videos", "videos"), ("Searches", "searches"))
        most = max((numbers.get(key) or 0 for _title, key in kinds), default=0)
        for title, key in kinds:
            value = numbers.get(key) or 0
            self._number(stopped, title, count(value),
                         fraction=value / most if most else 0, red=True)
        self._add(stopped)

        speed = Adw.PreferencesGroup(
            title="Filter speed",
            description=("How long the filter held each request before passing it on. "
                         "The time the website itself takes is not included."))
        lat = numbers.get("lat")
        if isinstance(lat, list) and len(lat) == usage_mod.BUCKETS and any(lat):
            self._lead(speed, self._speed_chart(lat, numbers))
        self._number(speed, "Average", millis(numbers.get("avg_ms")))
        self._number(speed, "Half of requests took no longer than",
                     millis(numbers.get("p50_ms")))
        self._number(speed, "95 in 100 took no longer than", millis(numbers.get("p95_ms")))
        self._add(speed)

        if self.uid == EVERYONE:
            self._add(self._by_person(ranged.get("people") or {}))

        if self.range == "all" and self.data.get("since"):
            since = datetime.date.fromisoformat(self.data["since"])
            note = Adw.PreferencesGroup(
                description=f"Counted since {since.day} {since:%B %Y}.")
            self._add(note)
        pointer_cursors(self.prefs)

    @staticmethod
    def _number(group: Adw.PreferencesGroup, title: str, value: str, *,
                fraction: float | None = None, red: bool = False) -> None:
        row = Adw.ActionRow(title=title)
        if fraction is not None:
            row.meter = charts.Meter(fraction, red=red)
            row.add_suffix(row.meter)
        label = Gtk.Label(label=value, xalign=1, width_chars=6)
        label.add_css_class("numeric")
        row.add_suffix(label)
        row.value = label
        group.add(row)

    # -- charts --------------------------------------------------------------------

    def _series(self, ranged: dict) -> tuple[list[str], list[dict]]:
        """The range's days, oldest first, and this view's numbers for each.
        Nothing for today and all time, or from a kosherd too old to say."""
        daily = ranged.get("daily")
        if not isinstance(daily, dict) or not daily.get("days"):
            return [], []
        days = list(daily["days"])
        if self.uid == EVERYONE:
            series = daily.get("everyone") or []
        else:
            series = (daily.get("people") or {}).get(str(self.uid)) or []
        series = [p if isinstance(p, dict) else {} for p in series]
        series += [{}] * (len(days) - len(series))
        return days, series[:len(days)]

    @staticmethod
    def _lead(group: Adw.PreferencesGroup, widget: Gtk.Widget) -> None:
        """Put a chart at the top of a group, above its rows."""
        group.add(widget)
        parent = widget.get_parent()
        if isinstance(parent, Gtk.Box):
            parent.reorder_child_after(widget, None)

    @staticmethod
    def _day_chart(title: str, days: list[str], values: list, say,
                   *, red: bool = False, empty: str = "Nothing counted") -> Gtk.Widget:
        values = [v if isinstance(v, int) and v > 0 else 0 for v in values]
        week = len(days) <= 7
        under = []
        for i, day in enumerate(days):
            if i == len(days) - 1:
                under.append("Today")
            elif week:
                under.append(f"{datetime.date.fromisoformat(day):%a}")
            else:
                # A label a week apart, counted back from today.
                under.append(charts.day_label(day) if (len(days) - 1 - i) % 7 == 0 else "")
        tips = [f"{charts.day_label(day, long=True)}: {say(v)}" for day, v in zip(days, values)]
        chart = charts.Columns(title, values, under, tips, red=red, empty=empty)
        first, last = charts.day_label(days[0]), charts.day_label(days[-1])
        if any(values):
            peak = max(range(len(values)), key=lambda i: values[i])
            chart.describe(f"{first} to {last}. Most on "
                           f"{charts.day_label(days[peak], long=True)}: {say(values[peak])}. "
                           f"In all, {say(sum(values))}.")
        else:
            chart.describe(f"{first} to {last}: none.")
        return charts.card(chart, title)

    @staticmethod
    def _speed_chart(lat: list[int], numbers: dict) -> Gtk.Widget:
        bounds = usage_mod.LATENCY_BOUNDS_MS
        under, tips = [], []
        total = sum(lat) or 1
        for i, n in enumerate(lat):
            share = f"{n / total:.0%}" if n else "none"
            if i < len(bounds):
                under.append(_bound(bounds[i], unit=i in (0, 9)))
                tips.append(f"Up to {millis(bounds[i])}: {count(n)} ({share})")
            else:
                under.append("more")
                tips.append(f"Over {millis(bounds[-1])}: {count(n)} ({share})")
        title = "Requests by how long the filter took"
        chart = charts.Columns(title, lat, under, tips, height=130)
        chart.describe(f"Half took no longer than {millis(numbers.get('p50_ms'))}; "
                       f"95 in 100 no longer than {millis(numbers.get('p95_ms'))}.")
        return charts.card(chart, title)

    def _by_person(self, people: dict) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup(title="By person")
        most = max(((people.get(str(u["uid"])) or {}).get("requests") or 0
                    for u in self._users()), default=0)
        for user in self._users():
            numbers = people.get(str(user["uid"])) or {}
            name = self._name(user["uid"])
            loaded = numbers.get("requests") or 0
            if loaded or counted(user):
                subtitle = (f"{count(loaded)} loaded · "
                            f"{size(numbers.get('bytes_in'))} · "
                            f"{labels.plural(numbers.get('blocked') or 0, 'page')} blocked")
            else:
                subtitle = "Not counted on this internet setting"
            row = Adw.ActionRow(title=name, subtitle=subtitle, activatable=True)
            row.add_prefix(avatar(name, 32))
            if loaded or counted(user):
                row.meter = charts.Meter(loaded / most if most else 0)
                row.add_suffix(row.meter)
            row.add_suffix(Gtk.Image(icon_name="go-next-symbolic"))
            row.connect("activated", lambda _r, uid=user["uid"]: self.show_user(uid))
            group.add(row)
        return group

    def _not_counted(self, user: dict) -> None:
        group = Adw.PreferencesGroup()
        page = Adw.StatusPage(icon_name="network-wired-symbolic",
                              title="Nothing to count",
                              description=not_counted_why(user))
        page.add_css_class("compact")
        group.add(page)
        self._add(group)
