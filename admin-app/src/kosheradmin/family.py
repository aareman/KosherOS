"""The guest account's own page, and the shape the rest of the app sees it in.

The family board that used to live here — one card per person — is gone:
the sidebar lists the family by name, and the family said the cards
duplicated it. What is left is the guest: `guest_user` turns the policy's
guest block into the same dict a user has, so every filter tab works on
it, and `GuestPage` is what a guest that is switched off opens onto.
"""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk  # noqa: E402

from . import labels  # noqa: E402
from .computer import _Page, health_rows  # noqa: E402,F401  (re-exported)
from .dialogs import WhitelistDialog  # noqa: E402


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


# The guest's kinds of internet. The daemon gives each kind its complete
# content settings (profiles.MODE_DEFAULTS): a guest is a stranger to the
# filter, and the strict answer is the one that is right without knowing
# them. Kept as a map so the page's order is explicit.
GUEST_KINDS = {
    "none": "none",
    "whitelist": "whitelist",
    "dnsfilter": "dnsfilter",
    "filtered": "filtered",
    "unfiltered": "unfiltered",
}
GUEST_KIND_HINTS = {
    "none": "No web at all for whoever signs in as the guest.",
    "whitelist": "Only the approved sites below, no pictures from the web, no YouTube.",
    "dnsfilter": "Known bad sites blocked and safe search forced; pages and pictures not checked.",
    "filtered": "Filtered, strictly: adult, gambling, social and video blocked, "
                "immodest pictures hidden, bad language replaced.",
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
            if guest_user(win.policy) is not None:
                win.go_to("guest")

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
        # group: nobody knows who the guest is. Each kind carries complete
        # content settings on the daemon's side, so a filtered guest still
        # gets pictures, language and YouTube handled.
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


