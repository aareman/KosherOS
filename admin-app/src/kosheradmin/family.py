"""The family board: the home screen.

One card per person, each the same four lines in the same order (web,
pictures, video, apps) so the eye compares across children, with what
happened today at the bottom. Above it, two banners that appear only when
they have something to say: the requests waiting for an answer (blue, the
one thing anyone is blocked on) and the filter's health (amber, the one
thing that is dangerous), whose Details goes to Protection in the sidebar.
Everything that is not about a person lives there now, in computer.py.
"""

from __future__ import annotations

import time

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk  # noqa: E402

from kosherd import profiles as profiles_mod  # noqa: E402

from . import labels  # noqa: E402
from .common import (avatar, clear, icon_line, mode_badge,  # noqa: E402
                     pointer_cursors, tag)
from .computer import _Page, health_rows  # noqa: E402,F401  (re-exported)
from .dialogs import RequestsDialog, WhitelistDialog  # noqa: E402


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
        self.health_banner.connect("button-clicked",
                                   lambda _b: self.win.go_to("protection"))
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
        # One height for every card, the guest's included. A homogeneous
        # FlowBox gives every child the same cell, but a card that asks for
        # less did not fill it; a vertical size group makes them all ask
        # for the tallest one's height.
        self.card_heights = Gtk.SizeGroup(mode=Gtk.SizeGroupMode.VERTICAL)
        time_usage = getattr(win, "time_usage", None) or {}
        for user in win.policy.get("users", []):
            counts = (win.summary or {}).get(str(user["uid"]))
            card = person_card(user, counts, by_uid.get(user["uid"], 0), custom,
                               time_usage.get(str(user["uid"])))
            self.card_heights.add_widget(card)
            child = Gtk.FlowBoxChild()
            child.set_child(card)
            child.user = user
            self.cards.append(child)
        guest_box = guest_card(win.policy.get("guest") or {"enabled": False}, custom)
        self.card_heights.add_widget(guest_box)
        guest = Gtk.FlowBoxChild()
        guest.set_child(guest_box)
        guest.user = None
        self.cards.append(guest)

        pointer_cursors(self)

    # -- actions ------------------------------------------------------------------------

    def _on_card(self, _box, child) -> None:
        user = getattr(child, "user", None)
        if user is None:
            guest = guest_user(self.win.policy)
            if guest is None:
                self.win.push(GuestPage(self.win))   # off: turn it on first
            else:
                self.win.open_user_detail(guest)     # on: a page like anyone's
        else:
            self.win.open_user_detail(user)

    def open_requests(self) -> RequestsDialog:
        dialog = RequestsDialog(self.win, self.win.requests)
        dialog.present(self.win)
        return dialog



# -- cards ---------------------------------------------------------------------------------

def person_card(user: dict, counts: dict | None, waiting: int, custom=(),
                time_usage: dict | None = None) -> Gtk.Box:
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

    # The filter mode, big and coloured: the one thing a parent should be
    # able to read across the room, and amber when the account is not
    # being filtered at all.
    text, protects = labels.mode_badge(user)
    box.append(mode_badge(text, protects))

    lines = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
    for icon, text, protects in labels.protection_lines(user):
        lines.append(icon_line(icon, text, protects))
    # The fifth line: today's time, "1 h 20 min used of 2 h", amber when
    # nothing limits it.
    text, protects = labels.time_line(user, time_usage)
    lines.append(icon_line("alarm-symbolic", text, protects))
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


def guest_user(policy: dict) -> dict | None:
    """The guest as the detail page sees an account, or None while it is
    off. Same fields as a user, so every filter tab works on it; the
    'guest' flag hides what a guest cannot have (apps, admin, removal)."""
    guest = policy.get("guest") or {}
    if not guest.get("enabled") or guest.get("uid") is None:
        return None
    return {"uid": guest["uid"], "username": "Guest", "guest": True,
            "mode": guest.get("mode", "whitelist"),
            "whitelist": list(guest.get("whitelist", [])),
            "rules": list(guest.get("rules", [])),
            "blocked_categories": list(guest.get("blocked_categories", [])),
            "media_level": guest.get("media_level", "none"),
            "language_filter": guest.get("language_filter", "off"),
            "youtube": dict(guest.get("youtube", {})),
            "cover_style": guest.get("cover_style", "frost"),
            "time": dict(guest.get("time", {})),
            "admin": False, "apps": [], "can_install_apps": False}


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
        badge, protects = labels.mode_badge(guest)
        box.append(mode_badge(badge, protects))
        text = "On. Passwordless; everything is wiped at sign-out."
    else:
        text = "Off. Passwordless, wiped at sign-out."
    sub = Gtk.Label(label=text, halign=Gtk.Align.CENTER, wrap=True, justify=2)
    sub.add_css_class("dim-label")
    sub.add_css_class("caption")
    box.append(sub)
    hint = Gtk.Label(label="Configure like any account" if enabled else "Turn on",
                     halign=Gtk.Align.CENTER)
    hint.add_css_class("accent")
    hint.add_css_class("caption")
    box.append(hint)
    box.append(Gtk.Box(vexpand=True))
    return box



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
            description="Anyone can sign in as the guest without a password, and "
                        "everything they did is erased at sign-out. Turn it on and "
                        "it gets a page like every other account: filter mode, "
                        "blocked kinds of sites, pictures, language, YouTube.")

        mode = guest.get("mode", "whitelist")
        wl_domains = guest.get("whitelist", [])

        def open_guest_page():
            fresh = guest_user(win.policy)
            if fresh is not None:
                win.pop_to_root()
                win.open_user_detail(fresh)

        def push(enabled, new_mode, domains):
            win.with_guardian(lambda pw: win.call(
                lambda: win.client.set_guest_config(enabled, new_mode, domains, pw),
                done_msg="Guest account is on" if enabled else "Guest settings saved",
                after_reload=open_guest_page if enabled else None))

        switch = Adw.SwitchRow(
            title="Guest account",
            subtitle="Turning it on opens its page, where everything can be set.",
            active=guest["enabled"])
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
            title="Start the guest off with",
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


