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
from kosherd import calm as calm_mod  # noqa: E402
from kosherd.language import OFF  # noqa: E402
from kosherd.policy import MEDIA_LEVEL_LABELS  # noqa: E402

APP_ID = "org.kosherlinux.MyFilter"

# Written for the person being filtered, not for the admin who configured
# it: what you can do, in the second person, without jargon. "dnsfilter"
# means nothing to a parent, let alone a child.
MODES = {
    "none": (
        "No internet",
        ("Internet access is blocked. Printers and other devices on the local network "
            "are still available."),
        "network-offline-symbolic",
    ),
    "whitelist": (
        "Approved sites only",
        ("You can visit the approved sites listed below. Use KosherOS Search to find "
            "pages on those sites."),
        "view-list-bullet-symbolic",
    ),
    "dnsfilter": (
        "DNS filter",
        ("Sites on the blocked lists are blocked before they load. Pages and pictures "
            "are not checked."),
        "network-wireless-signal-ok-symbolic",
    ),
    "filtered": (
        "Filtered internet",
        "Pages are checked as they load, using the settings below.",
        "security-high-symbolic",
    ),
    "unfiltered": (
        "Not filtered",
        ("Content filtering is off for this account. The ad and tracker setting is "
            "shown below."),
        "dialog-information-symbolic",
    ),
}

# String keys rather than the imported constants, so a test can read this
# table out of the source without importing GTK — the same reason the admin
# app spells its label tables out. A test asserts these keys stay in step
# with kosherd.language.MODES.
LANGUAGE = {
    "off": "Bad language is not filtered",
    "substitute": "Bad language is replaced",
    "block": "Pages with bad language are blocked",
}

# No "YouTube:" prefix — the row this fills is already titled YouTube, and
# the first render read "YouTube / YouTube: strictest setting".
YOUTUBE = {
    "strict": "Strict Restricted Mode",
    "moderate": "Moderate Restricted Mode",
    "off": "Restricted Mode off",
    "none": "Restricted Mode off",
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
    "none_left": "No time left today",
    "today": "Allowed hours today",
    "today_any": "Any time",
    "today_never": "Not today",
    "until": "Allowed until {clock}",
    "warning": "You will be warned 15 and 5 minutes before your time runs "
               "out, then signed out. Time the screen sits idle is not counted.",
}


def apps_words(s: dict) -> str:
    """What the Store offers you, in one line: the approved list or the
    whole store, and which kinds of app are blocked."""
    words = ("Store apps, except those blocked for your account"
             if s.get("app_access") == "store" else
             "Approved apps only")
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
        self.set_content(self.view)
        refresh = Gtk.Button(icon_name="view-refresh-symbolic",
                             tooltip_text="Refresh settings")
        refresh.connect("clicked", lambda _b: self.reload())
        self.refresh_button = refresh
        header = Adw.HeaderBar()
        header.pack_end(refresh)
        self.view.add_top_bar(header)
        self.reload()

    def reload(self) -> None:
        self.refresh_button.set_sensitive(False)

        # A spinner rather than an empty window: the call crosses D-Bus and
        # polkit, which is quick but not instant, and a window that appears
        # blank reads as broken.
        self.view.set_content(Adw.StatusPage(
            title="Loading your settings…", paintable=None,
            child=Gtk.Spinner(spinning=True, width_request=32,
                              height_request=32)))

        _run_async(lambda: DaemonClient().get_my_settings(),
                   self._on_loaded, self._on_error)

    # ---- states ----------------------------------------------------------

    def _on_error(self, e: Exception) -> None:
        self.refresh_button.set_sensitive(True)
        retry = Gtk.Button(label="Try again", halign=Gtk.Align.CENTER)
        retry.add_css_class("suggested-action")
        retry.connect("clicked", lambda _b: self.reload())
        page = Adw.StatusPage(
            icon_name="dialog-warning-symbolic",
            title="Could not load your settings",
            description=(f"{_error_text(e)}\n\nTry again. If this continues, "
                         "ask an administrator to check the filter status in KosherOS Admin."),
            child=retry)
        self.view.set_content(page)

    def _on_loaded(self, settings: dict) -> None:
        self.refresh_button.set_sensitive(True)
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
            mode, (mode or "Unknown", "This version of My Filter does not recognize this mode.",
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
        if blocked and mode in ("dnsfilter", "filtered"):
            group = Adw.PreferencesGroup(
                title="Blocked site categories",
                description="Sites in these categories are blocked.")
            for entry in blocked:
                group.add(Adw.ActionRow(title=entry["label"]))
            page.add(group)

        whitelist = s.get("whitelist")
        if whitelist is not None:
            group = Adw.PreferencesGroup(
                title="Sites you can visit",
                description=(f"{len(whitelist)} approved."
                             if whitelist else
                             "No sites approved yet. Ask an administrator to add a site."))
            for domain in whitelist:
                group.add(Adw.ActionRow(title=domain))
            page.add(group)

        page.add(self._footer(s))
        return page

    def _what_applies(self, s: dict) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup(title="Your settings")

        if "media_level" in s:
            level = s["media_level"]
            group.add(Adw.ActionRow(
                title="Pictures",
                subtitle=MEDIA_LEVEL_LABELS.get(level, level)))

        language = s.get("language_filter", OFF)
        mode = s.get("mode")
        language_text = LANGUAGE.get(language, language)
        if mode in ("none", "unfiltered"):
            language_text = "Language filtering does not apply in this mode"
        elif mode in ("dnsfilter", "whitelist") and language != OFF:
            language_text = "Searches with bad language are blocked; words on pages are not changed"
        group.add(Adw.ActionRow(
            title="Language",
            subtitle=language_text))

        youtube = s.get("youtube") or {}
        restrict = youtube.get("restrict")
        if restrict:
            allowed = youtube.get("allowed_channels") or []
            subtitle = YOUTUBE.get(restrict, restrict)
            if allowed:
                subtitle += (f" · {len(allowed)} approved "
                             + ("channel" if len(allowed) == 1 else "channels"))
            group.add(Adw.ActionRow(title="YouTube", subtitle=subtitle))

        calm = s.get("calm") or {}
        sites = calm_mod.enabled_sites(calm)
        if sites:
            names = ", ".join(calm_mod.label(k) for k in sites)
            subtitle = f"{names}: feeds end after {calm_mod.feed_items(calm)} items"
            budgets = [f"{calm_mod.label(k)} {calm_mod.daily_minutes(calm, k)} min a day"
                       for k in sites if calm_mod.daily_minutes(calm, k)]
            if budgets:
                subtitle += " · " + ", ".join(budgets)
            group.add(Adw.ActionRow(title="Calmer sites", subtitle=subtitle,
                                    subtitle_lines=3))

        group.add(Adw.ActionRow(
            title="Installing apps",
            subtitle=("You can install apps from KosherOS Store"
                      if s.get("can_install_apps") else
                      "Only an administrator can install apps")))
        if "app_access" in s:
            group.add(Adw.ActionRow(title="Available apps", subtitle=apps_words(s)))
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
            label=("Change these settings in KosherOS Admin."
                   if s.get("admin") else
                   "To change these settings, ask an administrator to open KosherOS Admin."),
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
