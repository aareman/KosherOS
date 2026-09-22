"""My Filter — what applies to your own account, in plain language.

Open to every user and read-only by construction: there is not one widget
here that writes anything. It exists because a filtered account should not
be a mystery to the person using it. A child who can see the rules is far
likelier to accept them than one who only ever meets a blocked page, and a
parent can sit down and go through this together with them.

Everything shown comes from a single `GetMySettings` call, where the
daemon takes the account from the D-Bus connection rather than from an
argument — so this window can only ever describe the person in front of
it.
"""

from __future__ import annotations

import threading

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

from kosherd.client import DaemonClient  # noqa: E402
from kosherd.language import OFF  # noqa: E402
from kosherd.policy import MEDIA_LEVEL_LABELS  # noqa: E402

APP_ID = "org.kosherlinux.MyFilter"

# Written for the person being filtered, not for the admin who configured
# it: what you can do, in the second person, without jargon. "dnsfilter"
# means nothing to a parent, let alone a child.
MODES = {
    "none": (
        "No internet",
        "This account cannot reach the internet. Printers and other "
        "computers in the house still work.",
        "network-offline-symbolic",
    ),
    "whitelist": (
        "Approved sites only",
        "You can visit the sites on the list below, and nothing else. "
        "Searching in KosherOS Search shows you what is on it.",
        "view-list-bullet-symbolic",
    ),
    "dnsfilter": (
        "Family filtering",
        "Sites known to be unsuitable are blocked before they load. "
        "Pages themselves are not inspected, so this is the lightest of "
        "the filters.",
        "network-wireless-signal-ok-symbolic",
    ),
    "filtered": (
        "Filtered browsing",
        "Pages are checked as they load. Unsuitable sites are blocked, "
        "pictures are looked at, and bad language is cleaned up.",
        "security-high-symbolic",
    ),
    "unfiltered": (
        "Not filtered",
        "This account has no content filtering. Ads and trackers are "
        "still blocked.",
        "dialog-information-symbolic",
    ),
}

# String keys rather than the imported constants, so a test can read this
# table out of the source without importing GTK — the same reason the admin
# app spells its label tables out. A test asserts these keys stay in step
# with kosherd.language.MODES.
LANGUAGE = {
    "off": "Bad language is not filtered",
    "substitute": "Bad language is replaced with hyphens",
    "block": "Pages with bad language are blocked",
}

# No "YouTube:" prefix — the row this fills is already titled YouTube, and
# the first render read "YouTube / YouTube: strictest setting".
YOUTUBE = {
    "strict": "Strictest setting",
    "moderate": "Moderate setting",
    "off": "No restriction",
}

# Your time on the computer. Shown to everyone, limited or not: "no limit"
# is an answer too, and a person who can see the limit and how much of
# today is left accepts the sign-out far more readily than one it ambushes.
TIME = {
    "title": "Your time",
    "unlimited": "No time limit",
    "admin": "No time limit — this is an administrator account",
    "limit": "Time each day",
    "left": "{left} left today",
    "used": "{used} used of {limit}",
    "none_left": "Today's time is used up",
    "today": "Today you can use this computer",
    "today_any": "Any time",
    "today_never": "Not today",
    "until": "Allowed until {clock}",
    "warning": "You will be warned 15 and 5 minutes before your time runs "
               "out, then signed out. Time the screen sits idle is not counted.",
}


def apps_words(s: dict) -> str:
    """What the Store offers you, in one line: the approved list or the
    whole store, and which kinds of app are blocked."""
    words = ("Anything in the store that is not blocked for you"
             if s.get("app_access") == "store" else
             "Only the apps an administrator has approved")
    kinds = [k.get("label") for k in s.get("blocked_app_kinds") or [] if k.get("label")]
    if kinds:
        words += " · blocked: " + ", ".join(kinds)
    return words


def _error_text(e: Exception) -> str:
    import re

    msg = re.sub(r"^.*?GDBus\.Error:[\w.]+: ", "", str(e))
    return re.sub(r" \(\d+\)$", "", msg)


def _run_async(work, on_done, on_error) -> None:
    def runner():
        try:
            result = work()
        except Exception as e:  # noqa: BLE001
            GLib.idle_add(on_error, e)
        else:
            GLib.idle_add(on_done, result)

    threading.Thread(target=runner, daemon=True).start()


class Window(Adw.ApplicationWindow):
    def __init__(self, app: Adw.Application):
        super().__init__(application=app, title="My Filter",
                         default_width=560, default_height=680)

        self.view = Adw.ToolbarView()
        self.view.add_top_bar(Adw.HeaderBar())
        self.set_content(self.view)

        # A spinner rather than an empty window: the call crosses D-Bus and
        # polkit, which is quick but not instant, and a window that appears
        # blank reads as broken.
        self.view.set_content(Adw.StatusPage(
            title="Reading your settings", paintable=None,
            child=Gtk.Spinner(spinning=True, width_request=32,
                              height_request=32)))

        _run_async(lambda: DaemonClient().get_my_settings(),
                   self._on_loaded, self._on_error)

    # ---- states ----------------------------------------------------------

    def _on_error(self, e: Exception) -> None:
        page = Adw.StatusPage(
            icon_name="dialog-warning-symbolic",
            title="Cannot read your settings",
            description=(f"{_error_text(e)}\n\nThe filter itself is not "
                         "affected by this — it runs in the system, not in "
                         "this window. Ask whoever manages this computer to "
                         "look at KosherOS Admin."))
        self.view.set_content(page)

    def _on_loaded(self, settings: dict) -> None:
        if not settings.get("managed", False):
            self.view.set_content(Adw.StatusPage(
                icon_name="dialog-information-symbolic",
                title="This account is not filtered",
                description=(
                    "No filter settings apply to this account. Ads and "
                    "trackers are still blocked for everyone on this "
                    "computer."
                    if settings.get("adblock", True) else
                    "No filter settings apply to this account.")))
            return
        self.view.set_content(self._build(settings))

    # ---- the page --------------------------------------------------------

    def _build(self, s: dict) -> Gtk.Widget:
        page = Adw.PreferencesPage()

        mode = s.get("mode", "")
        title, description, icon = MODES.get(
            mode, (mode or "Unknown", "This mode is not one this version of "
                   "My Filter knows how to describe.",
                   "dialog-question-symbolic"))

        banner = Adw.PreferencesGroup()
        row = Adw.ActionRow(title=title, subtitle=description)
        row.set_subtitle_lines(0)
        row.add_prefix(Gtk.Image(icon_name=icon, pixel_size=32))
        banner.add(row)
        page.add(banner)

        page.add(self._what_applies(s))
        if s.get("time") is not None:
            page.add(self._time(s["time"]))

        blocked = s.get("blocked_categories", [])
        if blocked:
            group = Adw.PreferencesGroup(
                title="Blocked subjects",
                description="Sites about these are not allowed.")
            for entry in blocked:
                group.add(Adw.ActionRow(title=entry["label"]))
            page.add(group)

        whitelist = s.get("whitelist")
        if whitelist is not None:
            group = Adw.PreferencesGroup(
                title="Sites you can visit",
                description=(f"{len(whitelist)} approved."
                             if whitelist else
                             "Nothing has been approved yet. Ask whoever "
                             "manages this computer to add a site."))
            for domain in whitelist:
                group.add(Adw.ActionRow(title=domain))
            page.add(group)

        page.add(self._footer(s))
        return page

    def _what_applies(self, s: dict) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup(title="What applies to you")

        if "media_level" in s:
            level = s["media_level"]
            group.add(Adw.ActionRow(
                title="Pictures",
                subtitle=MEDIA_LEVEL_LABELS.get(level, level)))

        language = s.get("language_filter", OFF)
        group.add(Adw.ActionRow(
            title="Language",
            subtitle=LANGUAGE.get(language, language)))

        youtube = s.get("youtube") or {}
        restrict = youtube.get("restrict")
        if restrict:
            allowed = youtube.get("allowed_channels") or []
            subtitle = YOUTUBE.get(restrict, restrict)
            if allowed:
                subtitle += f" · {len(allowed)} channels allowed"
            group.add(Adw.ActionRow(title="YouTube", subtitle=subtitle))

        group.add(Adw.ActionRow(
            title="Installing apps",
            subtitle=("You can install apps from KosherOS Store"
                      if s.get("can_install_apps") else
                      "Only an administrator can install apps")))
        if "app_access" in s:
            group.add(Adw.ActionRow(title="Which apps", subtitle=apps_words(s)))
        return group

    def _time(self, t: dict) -> Adw.PreferencesGroup:
        """How long, and when, you may use this computer — and how much of
        today is left. Everything here is the daemon's arithmetic
        (kosherd.timelimits); this window only puts words to it."""
        import time as time_mod

        from kosherd import timelimits

        group = Adw.PreferencesGroup(title=TIME["title"])
        if t.get("admin"):
            group.add(Adw.ActionRow(title=TIME["admin"]))
            return group
        if not t.get("limited"):
            group.add(Adw.ActionRow(title=TIME["unlimited"]))
            return group

        minutes = int(t.get("daily_minutes") or 0)
        if minutes:
            used = int(t.get("used") or 0)
            limit = minutes * 60
            subtitle = TIME["used"].format(used=timelimits.duration_text(used),
                                           limit=timelimits.duration_text(limit))
            left = max(0, limit - used)
            subtitle += " · " + (TIME["left"].format(left=timelimits.duration_text(left))
                                 if left else TIME["none_left"])
            group.add(Adw.ActionRow(title=TIME["limit"], subtitle=subtitle))

        today = t.get("today") or timelimits.ALWAYS
        if today == timelimits.ALWAYS:
            hours = TIME["today_any"]
        elif today == timelimits.NEVER:
            hours = TIME["today_never"]
        else:
            hours = timelimits.hours_text(today)
        if t.get("block_ends"):
            hours += " · " + TIME["until"].format(
                clock=time_mod.strftime("%H:%M", time_mod.localtime(t["block_ends"])))
        if today != timelimits.ALWAYS or minutes == 0:
            group.add(Adw.ActionRow(title=TIME["today"], subtitle=hours))

        note = Gtk.Label(label=TIME["warning"], wrap=True, xalign=0,
                         margin_top=6, margin_start=6, margin_end=6,
                         css_classes=["dim-label", "caption"])
        group.add(note)
        return group

    def _footer(self, s: dict) -> Adw.PreferencesGroup:
        # Two facts worth stating plainly rather than leaving to be
        # discovered: that this window cannot change anything, and that ad
        # blocking is not personal to this account.
        group = Adw.PreferencesGroup()
        group.add(Adw.ActionRow(
            title="Ads and trackers",
            subtitle=("Blocked for every account on this computer"
                      if s.get("adblock", True) else "Not blocked")))
        if s.get("admin"):
            group.add(Adw.ActionRow(
                title="This is an administrator account",
                subtitle="You can change settings in KosherOS Admin."))
        note = Gtk.Label(
            label="This window only shows your settings. It cannot change "
                  "them.",
            wrap=True, justify=Gtk.Justification.CENTER,
            margin_top=18, css_classes=["dim-label", "caption"])
        group.add(note)
        return group


class Application(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID)

    def do_activate(self):
        window = self.props.active_window or Window(self)
        window.present()


def main() -> int:
    return Application().run(None)
