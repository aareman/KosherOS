"""Every setting, in the words a parent would use.

The daemon speaks in keys ("dnsfilter", "immodest", "category:video"); the
app speaks in sentences. All of the translation lives here, in literal
tables, so a test can read them out of the source and check that every
value the daemon accepts has words — a dropdown that silently cannot reach
a value is the kind of bug nobody notices until a family hits it.
"""

from __future__ import annotations

import re
import time

from kosherd import appaccess
from kosherd.appkinds import KIND_LABELS
from kosherd.categories import CATEGORY_LABELS
from kosherd.policy import YOUTUBE_CATEGORIES

MODE_LABELS = {
    "none": "No internet",
    "whitelist": "Whitelist only",
    "dnsfilter": "DNS filter",
    "filtered": "Filtered internet",
    "unfiltered": "Unfiltered",
}

MODE_HINTS = {
    "none": "No web access at all. Printing and local network still work.",
    "whitelist": "Only the sites on the approved list, and safe search is forced.",
    "dnsfilter": "Basic protection only: blocks known bad sites and forces "
                 "safe search. Pages and pictures are NOT checked in this "
                 "mode — for that, use Filtered internet.",
    "filtered": "Safe search, page rules and content filtering. To judge "
                "pages this reads the connection, so this user's HTTPS is "
                "decrypted on this computer.",
    "unfiltered": "No filtering of any kind for this account.",
}

# Pictures and video, in the words a parent would use. The order matches
# MEDIA_LEVELS, from "leave everything" to "hide everything".
MEDIA_LABELS = {
    "none": "Show all pictures",
    "nsfw": "Hide explicit pictures",
    "suggestive": "Hide explicit and suggestive pictures",
    "immodest": "Hide immodest pictures too",
    "people": "Hide pictures of people too",
    "all": "Hide all pictures from the web",
}
MEDIA_HINTS = {
    "none": "No filtering of pictures or video.",
    "nsfw": "Hides pictures on pages that read as explicit.",
    "suggestive": "Also hides pictures on pages that read as suggestive.",
    "immodest": "Also hides pictures on pages about immodest dress. The "
                "strictest setting that still judges each picture.",
    "people": "Also hides any picture with a person in it, whatever they "
              "wear. Tight or sheer clothing is not something the filter can "
              "judge, and this is the honest answer to it: landscapes, "
              "products, diagrams and text still show.",
    "all": "No pictures from the web at all. Nothing here depends on a "
           "judgement call, which is why it is the only setting that is "
           "right every time.",
}
# The same levels as one short line on a card or a chip.
MEDIA_SHORT = {
    "none": "All pictures shown",
    "nsfw": "Explicit pictures hidden",
    "suggestive": "Suggestive pictures hidden",
    "immodest": "Immodest pictures hidden",
    "people": "Pictures of people hidden",
    "all": "No pictures from the web",
}

# How a picture that is kept but partly hidden gets covered.
COVER_LABELS = {
    "frost": "Frost the figure",
    "skin": "Paint over skin",
}
COVER_HINTS = {
    "frost": "The person is frosted out: the shape is gone, the rest of the "
             "picture and the page stay. The default.",
    "skin": "Skin inside the picture is painted a solid grey, with a margin; "
            "clothing and background stay. Where the colour check finds no "
            "skin to paint, the figure is frosted instead.",
}
COVER_ORDER = ("frost", "skin")

LANGUAGE_LABELS = {
    "off": "Leave bad language alone",
    "substitute": "Replace bad language with a milder word",
    "block": "Block pages with bad language",
}
LANGUAGE_SHORT = {
    "off": "Bad language left alone",
    "substitute": "Bad language replaced",
    "block": "Pages with bad language blocked",
}
LANGUAGE_ORDER = ("off", "substitute", "block")

# What the Store offers an account, as one short line.
APP_ACCESS_SHORT = {
    "approved": "Approved apps only",
    "store": "The whole store",
}


def app_name(ref: str) -> str:
    """'org.gnome.Chess' -> 'Chess': what to call an app when only its id
    is known, rather than showing a person a ref."""
    return (ref.rsplit(".", 1)[-1] or ref).strip() or ref


def apps_line(user: dict) -> tuple[str, bool]:
    """The apps chip: what this account may have, and whether that is a
    restriction (drawn blue) or the open store (drawn amber)."""
    access = appaccess.access_of(user)
    kinds = len(user.get("blocked_app_kinds") or [])
    singles = len(user.get("blocked_apps") or [])
    allowed = user.get("apps") or []   # the older allow-list, until converted
    if allowed:
        return plural(len(allowed), "app allowed"), True
    words = APP_ACCESS_SHORT[access]
    if kinds:
        words += f", {plural(kinds, 'kind')} blocked"
    elif singles:
        words += f", {plural(singles, 'app')} blocked"
    if user.get("can_install_apps", True):
        words += ", can install"
    return words, access == "approved" or bool(kinds or singles)

YOUTUBE_RESTRICT_LABELS = {
    "none": "Off",
    "moderate": "Moderate",
    "strict": "Strict",
}
YOUTUBE_RESTRICT_ORDER = ("none", "moderate", "strict")

# A channel's handle or ID, alone or inside any of its addresses — what the
# filter matches on. youtube.com/c/… and /user/… names are not here: the
# filter cannot match them, so they are searched for like any other name.
_CHANNEL_HANDLE = re.compile(r"(?:^|youtube\.com/)(@[\w.-]+)", re.IGNORECASE)
_CHANNEL_ID = re.compile(r"(?:^|/channel/)(UC[\w-]{22})(?:$|[/?#])")


def youtube_channel_ref(text: str) -> str | None:
    """'@handle' or 'UC…' from what an admin pasted — the handle or ID
    itself, or an address of the channel or of its page — or None when it
    is a name to search for instead."""
    text = (text or "").strip()
    for pattern in (_CHANNEL_HANDLE, _CHANNEL_ID):
        match = pattern.search(text)
        if match:
            return match.group(1)
    return None


def channel_result_subtitle(channel: dict) -> str:
    """'@torahanytime1 · 12.8K subscribers' for a search result, falling
    back to the ID so two channels with one name can be told apart."""
    parts = [p for p in (channel.get("handle"), channel.get("subscribers")) if p]
    return " · ".join(parts) or channel["id"]

# How the desktop looks, in the order a parent would consider them. Not a
# protection — the filter does not care which shell draws the windows — so
# no guardian password, and it says so plainly.
LAYOUT_LABELS = {
    "classic": "Classic desktop",
    "tiling": "Tiling desktop",
    "advanced": "Advanced (niri)",
}
LAYOUT_HINTS = {
    "classic": "A taskbar along the bottom, a start button, windows that "
               "minimise. What a Windows, Mac or ChromeOS user already knows. "
               "The right choice for almost everyone.",
    "tiling": "Windows arrange themselves in a scrolling row and are "
              "driven from the keyboard (PaperWM). Same lock screen, "
              "settings and accessibility as the classic desktop.",
    "advanced": "A separate session for power users, picked at the login "
                "screen: the niri window manager with the Noctalia shell, "
                "configured by text files. No GNOME Settings, and less "
                "accessibility support. Filtering is unaffected.",
}
LAYOUT_ORDER = ("classic", "tiling", "advanced")

NO_GROUP = "No group"

# -- time -----------------------------------------------------------------------

# The daily limits offered with one click, keyed by minutes; 0 is no limit.
# A test keeps the keys in step with timelimits.LIMIT_PRESET_MINUTES.
TIME_LIMIT_LABELS = {
    30: "30 minutes a day",
    60: "1 hour a day",
    120: "2 hours a day",
    180: "3 hours a day",
    0: "No daily limit",
}
TIME_LIMIT_ORDER = (0, 30, 60, 120, 180)
TIME_LIMIT_CUSTOM = "A different amount…"
TIME_LIMIT_HINT = ("Counts the time this person is signed in and using the "
                   "computer; a screen left idle is not counted. They are "
                   "warned 15 and 5 minutes before it runs out, then signed "
                   "out. The count starts again at midnight.")

# One-click starting points for the calendar, keyed like
# timelimits.SCHEDULE_PRESETS; a parent paints from there.
SCHEDULE_PRESET_LABELS = {
    "always": "Always",
    "after_school": "After school",
    "not_late": "Not late at night",
    "weekdays": "Weekdays only",
}
SCHEDULE_PRESET_HINTS = {
    "always": "Any hour of any day.",
    "after_school": "School days from 3 pm to 8 pm, weekends from 9 am to 8 pm.",
    "not_late": "Every day from 6 am to 9 pm.",
    "weekdays": "Monday to Friday at any hour; not at the weekend.",
}
SCHEDULE_PRESET_ORDER = ("always", "after_school", "not_late", "weekdays")
SCHEDULE_TITLE = "When this account may be used"
SCHEDULE_HINT = ("Click or drag across the calendar to paint the hours. Amber "
                 "hours are allowed; blue hours are not. Signing in outside "
                 "the amber hours is refused, and a session that runs into a "
                 "blue hour is warned and then signed out.")
SCHEDULE_LEGEND_ALLOWED = "Allowed"
SCHEDULE_LEGEND_BLOCKED = "Not allowed"
TIME_ADMIN_NOTE = ("Administrators are never limited. A parent must always be "
                   "able to sign in and change a setting; to limit this "
                   "person, make the account a user account first.")
TIME_TODAY_TITLE = "Today"
TIME_TAB_INTRO = ("How long, and when, this person may use the computer. "
                  "Nothing here is set until you set it.")
# Why a session ended, for the activity feed (activity.TIME events).
TIME_WHY = {
    "time:limit": "today's time was used up",
    "time:schedule": "the allowed hours ended",
    "time:login": "a sign-in outside the allowed time was refused",
}

MODE_NOTHING_APPLIES = {
    "none": "This account has no internet access, so nothing here applies.",
    "unfiltered": "An unfiltered account enforces nothing.",
}

# The lists a family may add to or take from, in their words. Everything
# here is machine-wide, not per account: a word is either bad language in
# this house or it is not.
EDITABLE_LISTS = (
    ("wordlist.json", "Bad language",
     "Words replaced with a milder one on the page, for accounts set to "
     "clean up bad language.", "word"),
    ("search-blocklist.json", "Blocked searches",
     "Searches that will not run at all, because the results page would "
     "show the words even with every link removed.", "search"),
    ("content-terms.json", "Words pages are judged by",
     "Words that count as evidence when deciding whether a page is "
     "inappropriate. No single one blocks a page on its own.", "term"),
)

# Where a term a family adds sits on the ladder, in plain words. Weights
# are deliberately not exposed: "how bad is it" is a question a parent can
# answer and "how many points is it worth" is not.
CONTENT_LEVELS = (
    ("immodest", "Immodest", 12),
    ("suggestive", "Suggestive", 12),
    ("nsfw", "Explicit", 25),
)

# What to say when picture checking is not doing what the settings claim.
# Silence would be the worst option: a family that sees blank pictures and
# no explanation concludes the filter is broken and turns it off.
PICTURE_STATE = {
    "too_slow": (
        "This computer is hiding pictures instead of checking them",
        "Checking each picture takes about {ms} ms here, which would make "
        "pages slow to load and still leave many unchecked. Hiding them is "
        "faster and never wrong. Set “Pictures and video” to “Show all "
        "pictures” if you would rather have them."),
    "no_model": (
        "Pictures are being hidden, not checked",
        "The picture model is not installed on this computer, so any "
        "account set to filter pictures hides them instead."),
}

CONTENT_LEVEL_WORDS = {"nsfw": "explicit", "suggestive": "suggestive",
                       "immodest": "immodest"}


def why_text(why: str) -> str:
    """'category:video,social' -> 'Video and streaming, Social networks'.

    The reason the filter gives for a block, in the family's words, for the
    activity feed and beside a request.
    """
    if not why:
        return ""
    kind, _, rest = why.partition(":")
    if kind == "category":
        names = [n for n in rest.split(",") if n]
        return ", ".join(CATEGORY_LABELS.get(n, n) for n in names)
    if kind == "rule":
        return "a page rule" + (f" ({rest})" if rest else "")
    if kind == "content":
        return "the page reads as " + CONTENT_LEVEL_WORDS.get(rest, rest or "inappropriate")
    if kind == "language":
        return "bad language on the page"
    if kind == "shop":
        return rest or "a blocked department"
    if kind == "youtube":
        return ("not an approved channel" if rest == "channel"
                else "that kind of video is turned off")
    if kind == "time":
        return TIME_WHY.get(why, "time was up")
    return why


def when_text(t: int, now: float | None = None) -> str:
    """'20 min ago', '2 hours ago', 'yesterday 21:30', 'Sun 17:02'."""
    now = time.time() if now is None else now
    delta = max(0, int(now - t))
    if delta < 60:
        return "just now"
    if delta < 3600:
        return f"{delta // 60} min ago"
    if delta < 6 * 3600:
        hours = delta // 3600
        return f"{hours} hour{'s' if hours != 1 else ''} ago"
    then = time.localtime(t)
    today = time.localtime(now)
    clock = time.strftime("%H:%M", then)
    if (then.tm_year, then.tm_yday) == (today.tm_year, today.tm_yday):
        return clock
    yesterday = time.localtime(now - 86400)
    if (then.tm_year, then.tm_yday) == (yesterday.tm_year, yesterday.tm_yday):
        return f"yesterday {clock}"
    if delta < 6 * 86400:
        return time.strftime("%a ", then) + clock
    return time.strftime("%-d %b ", then) + clock


def until_text(t: int, now: float | None = None) -> str:
    """A moment ahead: '21:00', 'tomorrow 06:00', 'Mon 06:00'."""
    now = time.time() if now is None else now
    then = time.localtime(t)
    today = time.localtime(now)
    clock = time.strftime("%H:%M", then)
    if (then.tm_year, then.tm_yday) == (today.tm_year, today.tm_yday):
        return clock
    tomorrow = time.localtime(now + 86400)
    if (then.tm_year, then.tm_yday) == (tomorrow.tm_year, tomorrow.tm_yday):
        return f"tomorrow {clock}"
    return time.strftime("%a ", then) + clock


def day_label(t: int, now: float | None = None) -> str:
    """The heading a feed entry files under: Today, Yesterday, Sunday, 3 Sep."""
    now = time.time() if now is None else now
    then = time.localtime(t)
    today = time.localtime(now)
    if (then.tm_year, then.tm_yday) == (today.tm_year, today.tm_yday):
        return "Today"
    yesterday = time.localtime(now - 86400)
    if (then.tm_year, then.tm_yday) == (yesterday.tm_year, yesterday.tm_yday):
        return "Yesterday"
    if now - t < 6 * 86400:
        return time.strftime("%A", then)
    return time.strftime("%-d %B", then)


def host_of(url: str) -> str:
    from urllib.parse import urlsplit

    try:
        host = urlsplit(url).hostname or url
    except ValueError:
        return url
    return host.removeprefix("www.")


def short_url(url: str, limit: int = 60) -> str:
    """'https://www.youtube.com/watch?v=abc...' -> 'youtube.com/watch?v=abc…'."""
    from urllib.parse import urlsplit

    try:
        parts = urlsplit(url)
    except ValueError:
        return url[:limit]
    host = (parts.hostname or "").removeprefix("www.")
    path = parts.path.rstrip("/")
    if parts.query:
        path += "?" + parts.query
    text = host + path
    return text if len(text) <= limit else text[:limit - 1] + "…"


def plural(n: int, noun: str, plural_form: str | None = None) -> str:
    return f"{n} {noun if n == 1 else (plural_form or noun + 's')}"


# -- what changed --------------------------------------------------------------

def change_sentence(event: dict, users_by_uid: dict | None = None) -> tuple[str, str]:
    """A change-log event as (what, who-and-when).

    `event` is one CHANGE record from the daemon: method, args (with any
    password already stripped), by/by_username, uid/username, guardian.
    """
    method = event.get("method", "")
    args = event.get("args") or []
    who = event.get("username") or "?"
    actor = event.get("by_username") or "an administrator"

    def arg(i, default=None):
        return args[i] if len(args) > i else default

    if method == "SetFilterMode":
        what = f"{who}: filter mode → {MODE_LABELS.get(arg(1), arg(1))}"
    elif method == "ApplyProfile":
        what = (f"{who}: put in the {_preset_label(arg(1), users_by_uid)} group"
                if arg(1) else f"{who}: taken out of their group")
    elif method == "SetBlockedCategories":
        cats = arg(1) or []
        what = f"{who}: {plural(len(cats), 'kind')} of site blocked"
    elif method == "SetMediaLevel":
        what = f"{who}: pictures → {MEDIA_SHORT.get(arg(1), arg(1)).lower()}"
    elif method == "SetLanguageFilter":
        what = f"{who}: {LANGUAGE_SHORT.get(arg(1), arg(1)).lower()}"
    elif method == "SetYouTube":
        what = f"{who}: YouTube settings changed"
    elif method == "SetUserAdmin":
        what = f"{who} is {'now' if arg(1) else 'no longer'} an administrator"
    elif method == "SetLayout":
        what = f"{who}: desktop → {LAYOUT_LABELS.get(arg(1), arg(1))}"
    elif method == "SetCoverStyle":
        what = f"{who}: covered pictures → {COVER_LABELS.get(arg(1), arg(1)).lower()}"
    elif method == "SetWhitelist":
        what = f"{who}: approved-sites list changed"
    elif method == "SetUrlRules":
        what = f"{who}: page rules changed"
    elif method == "AllowUrl":
        what = f"{who} may now reach {host_of(str(arg(1) or ''))}" + \
            (" (whole site)" if arg(2) else "")
    elif method == "ApproveRequest":
        what = "A request was allowed"
    elif method == "DismissRequest":
        what = "A request was turned down"
    elif method == "SaveProfile":
        what = f"Group “{arg(1)}” saved from {who}; every account in it follows"
    elif method == "DeleteProfile":
        what = f"Group {_preset_label(arg(0), users_by_uid)} deleted"
    elif method == "EditList":
        what = f"Word list edited ({_list_title(arg(0))})"
    elif method == "SetAdBlock":
        what = "Ads and trackers " + ("blocked for everyone" if arg(0) else "no longer blocked")
    elif method == "SetNetworkAccess":
        ports = arg(2) if isinstance(arg(2), list) else []
        what = f"{who}: video calls {'on' if arg(1) else 'off'}" + \
            (f", extra ports {', '.join(str(p) for p in ports)}" if ports else ", no extra ports")
    elif method == "SetTimeLimits":
        what = f"{who}: time → {time_summary(arg(1) if isinstance(arg(1), dict) else {})}"
    elif method == "SetGuestConfig":
        what = "Guest account " + ("turned on" if arg(0) else "turned off") + \
            (f", {MODE_LABELS.get(arg(1)) or _preset_label(arg(1), users_by_uid)}"
             if arg(0) and arg(1) else "")
    elif method == "CreateUser":
        what = f"Account {arg(0)} created"
    elif method == "AdoptUser":
        what = f"Account {arg(0)} brought under management"
    elif method == "ResetPassword":
        what = f"{who}: password reset — a new one is chosen at their next sign-in"
    elif method == "RemoveUser":
        what = f"Account {who} removed"
    elif method == "SetUserApps":
        what = f"{who}: which apps are allowed changed"
    elif method == "SetUserCanInstall":
        what = f"{who} {'may' if arg(1) else 'may not'} install apps"
    elif method == "SetUserAppAccess":
        what = f"{who}: {APP_ACCESS_SHORT.get(arg(1), str(arg(1)))}"
    elif method == "SetUserBlockedAppKinds":
        kinds = arg(1) or []
        what = f"{who}: {plural(len(kinds), 'kind')} of app blocked"
    elif method == "SetUserBlockedApps":
        refs = arg(1) or []
        what = f"{who}: {plural(len(refs), 'app')} blocked by name"
    elif method == "RemoveApp":
        what = f"App uninstalled for everyone ({arg(0)})"
    elif method == "ApproveApp":
        what = f"App approved: {arg(1) or arg(0)}"
    elif method == "UnapproveApp":
        what = f"App no longer approved ({arg(0)})"
    elif method == "SetCaptiveMode":
        what = f"{who}: Wi-Fi sign-in window opened for {arg(1)} minutes"
    elif method == "SetGuardianPassword":
        what = "Guardian password set"
    elif method == "DisableGuardian":
        what = "Guardian password turned off"
    elif method == "Enrol":
        what = "Computer enrolled with a portal"
    elif method == "Unenrol":
        what = "Computer unenrolled from the portal"
    elif method == "SetChannel":
        what = f"Updates now come from the {arg(0)} stream"
    elif method == "ApplyUpdate":
        what = "A system update was downloaded"
    elif method == "Rollback":
        what = "The computer was put back to its previous version"
    else:
        what = method or "A setting changed"
    detail = f"{actor}, {when_text(event.get('t', 0))}"
    if event.get("guardian"):
        detail += " · guardian password given"
    return what, detail


def _preset_label(key, users_by_uid) -> str:
    """A group's name from its key, or the key when the group is gone."""
    from kosherd import profiles

    try:
        return profiles.get(str(key), (users_by_uid or {}).get("custom_profiles", [])).label
    except (KeyError, TypeError):
        return str(key)


def _list_title(name) -> str:
    for list_name, title, _d, _n in EDITABLE_LISTS:
        if list_name == name:
            return title
    return str(name)


# -- how an account departs from its group --------------------------------------

def drift_sentences(changes: list[dict]) -> list[str]:
    """profiles.diff() changes, each as one short line."""
    out: list[str] = []
    for change in changes:
        field = change.get("field")
        if field == "blocked_categories":
            for name in change.get("added", []):
                out.append(f"{CATEGORY_LABELS.get(name, name)} also blocked")
            for name in change.get("removed", []):
                out.append(f"{CATEGORY_LABELS.get(name, name)} not blocked")
        elif field == "media_level":
            out.append(f"Pictures: {MEDIA_SHORT.get(change.get('to'), change.get('to')).lower()}")
        elif field == "language_filter":
            out.append(LANGUAGE_SHORT.get(change.get("to"), str(change.get("to"))))
        elif field == "can_install_apps":
            out.append("Can install apps" if change.get("to") else "Cannot install apps")
        elif field == "app_access":
            out.append(APP_ACCESS_SHORT.get(change.get("to"), str(change.get("to"))))
        elif field == "blocked_app_kinds":
            for name in change.get("added", []):
                out.append(f"{KIND_LABELS.get(name, name)} apps also blocked")
            for name in change.get("removed", []):
                out.append(f"{KIND_LABELS.get(name, name)} apps not blocked")
        elif field == "blocked_apps":
            added, removed = change.get("added", []), change.get("removed", [])
            if added:
                out.append(f"{plural(len(added), 'more app')} blocked")
            if removed:
                out.append(f"{plural(len(removed), 'app')} unblocked")
        elif field == "youtube":
            out.extend(_youtube_drift(change.get("from") or {}, change.get("to") or {}))
    return out


def _youtube_drift(before: dict, after: dict) -> list[str]:
    out = []
    if before.get("restrict", "moderate") != after.get("restrict", "moderate"):
        out.append("YouTube Restricted Mode: "
                   + YOUTUBE_RESTRICT_LABELS.get(after.get("restrict"), str(after.get("restrict"))))
    b_kinds = set(before.get("blocked_categories") or [])
    a_kinds = set(after.get("blocked_categories") or [])
    for code in sorted(a_kinds - b_kinds):
        out.append(f"YouTube: {YOUTUBE_CATEGORIES.get(code, code)} also blocked")
    for code in sorted(b_kinds - a_kinds):
        out.append(f"YouTube: {YOUTUBE_CATEGORIES.get(code, code)} allowed")
    b_ch = set(before.get("allowed_channels") or [])
    a_ch = set(after.get("allowed_channels") or [])
    if a_ch != b_ch:
        out.append(f"YouTube: {plural(len(a_ch), 'approved channel')}"
                   if a_ch else "YouTube: every channel allowed")
    return out


def setup_sentence(key: str | None, changes: list[dict], custom=()) -> str:
    """'In the Kids group' / 'In the Kids group, with 2 changes' / 'Not in a group'."""
    from kosherd import profiles

    if key is None:
        return "Not in a group"
    label = profiles.get(key, custom).label
    n = len(drift_sentences(changes))
    if not n:
        return f"In the {label} group"
    return f"In the {label} group, with {plural(n, 'change')}"


# -- the four lines on a card, in a fixed order --------------------------------

# How the mode reads on a badge, and whether that mode is protecting.
# The mode is the one thing a parent must be able to see without reading,
# so it is drawn as a badge rather than a line of text, and an account that
# is not being filtered is amber rather than blue.
MODE_BADGE = {
    "none": "No internet",
    "whitelist": "Approved sites only",
    "dnsfilter": "Basic protection",
    "filtered": "Filtered internet",
    "unfiltered": "Not filtered",
}
MODE_PROTECTS = {"none": True, "whitelist": True, "dnsfilter": True,
                 "filtered": True, "unfiltered": False}


def mode_badge(user: dict) -> tuple[str, bool]:
    """(what the badge says, whether it is protecting)."""
    mode = user.get("mode", "filtered")
    return MODE_BADGE.get(mode, MODE_LABELS.get(mode, mode)), MODE_PROTECTS.get(mode, True)


def protection_lines(user: dict) -> list[tuple[str, str, bool]]:
    """(icon, text, protects) × 4: web, pictures, video, apps. Always the
    same order, so the eye can compare across the family; the last element
    says whether that line is blocking something (drawn blue) or leaving it
    open (drawn amber), which is what a parent is scanning for."""
    mode = user.get("mode", "filtered")
    kinds = len(user.get("blocked_categories") or [])
    if mode == "whitelist":
        bundles = len(user.get("whitelist_bundles") or [])
        own = len(user.get("whitelist") or [])
        if bundles and own:
            web = f"Only {plural(bundles, 'approved list')} and {plural(own, 'site')}"
        elif bundles:
            web = f"Only {plural(bundles, 'approved list')} of sites"
        else:
            web = f"Only {plural(own, 'approved site')}"
    elif mode == "none":
        web = "No internet"
    elif mode == "unfiltered":
        web = "Unfiltered internet"
    elif mode == "dnsfilter":
        web = f"Basic protection · {plural(kinds, 'kind')} of site blocked"
    else:
        web = f"Filtered internet · {plural(kinds, 'kind')} of site blocked"

    if mode in ("none", "unfiltered"):
        pictures = "Pictures not checked" if mode == "unfiltered" else "No web, no pictures"
    else:
        pictures = MEDIA_SHORT.get(user.get("media_level", "none"), "All pictures shown")

    youtube = user.get("youtube") or {}
    if mode in ("none", "whitelist"):
        video = "No YouTube"
    elif mode != "filtered":
        video = "YouTube unfiltered"
    elif youtube.get("allowed_channels"):
        video = f"YouTube: {plural(len(youtube['allowed_channels']), 'approved channel')} only"
    else:
        restrict = YOUTUBE_RESTRICT_LABELS.get(youtube.get("restrict", "moderate"), "Moderate")
        blocked = len(youtube.get("blocked_categories") or [])
        video = f"YouTube {restrict.lower()}" + (f", {plural(blocked, 'kind')} blocked" if blocked else "")

    apps, apps_protect = apps_line(user)

    mode_protects = MODE_PROTECTS.get(mode, True)
    return [
        ("web-browser-symbolic", web, mode_protects),
        ("image-x-generic-symbolic", pictures,
         mode_protects and user.get("media_level", "none") != "none"),
        ("video-display-symbolic", video,
         mode in ("none", "whitelist") or (mode == "filtered" and (
             bool(youtube.get("allowed_channels")) or bool(youtube.get("blocked_categories"))
             or youtube.get("restrict", "moderate") != "none"))),
        ("view-grid-symbolic", apps, apps_protect),
    ]


# -- time, on a card and a chip --------------------------------------------------

def time_sentence(time_settings: dict | None, usage: dict | None) -> str:
    """'1 h 20 min used of 2 h' / '45 min today, no daily limit' / 'No daily limit'."""
    from kosherd import timelimits

    minutes = timelimits.daily_minutes(time_settings)
    used = int((usage or {}).get("used", 0) or 0)
    if minutes:
        return (f"{timelimits.duration_text(used)} used of "
                f"{timelimits.duration_text(minutes * 60)}")
    if used:
        return f"{timelimits.duration_text(used)} today, no daily limit"
    return "No daily limit"


def time_today(time_settings: dict | None, now: float | None = None) -> str:
    """'today 15:00–20:00' / 'not today' / '' when any hour is fine."""
    from kosherd import timelimits

    grid = timelimits.grid(time_settings)
    today = grid[time.localtime(now).tm_wday]
    if today == timelimits.ALWAYS:
        return ""
    if today == timelimits.NEVER:
        return "not today"
    return "today " + timelimits.hours_text(today)


def time_line(user: dict, usage: dict | None) -> tuple[str, bool]:
    """(text, protects) — the one line about time on a card and its chip.
    Blue when a limit or a schedule holds; amber when nothing does."""
    from kosherd import timelimits

    if user.get("admin"):
        return "No time limit (administrator)", False
    settings = user.get("time") or {}
    text = time_sentence(settings, usage)
    today = time_today(settings)
    if today:
        text += " · " + today
    return text, timelimits.is_limited(settings)


def time_summary(time_settings: dict | None) -> str:
    """'2 h a day, chosen hours' — a setting in five words, for the change log."""
    from kosherd import timelimits

    minutes = timelimits.daily_minutes(time_settings)
    parts = [f"{timelimits.duration_text(minutes * 60)} a day" if minutes else "no daily limit",
             "any hour" if timelimits.is_always(timelimits.grid(time_settings))
             else "chosen hours"]
    return ", ".join(parts)


# What the feed says when time, not content, ended something.
TIME_EVENT_TITLES = {
    "time:limit": "Signed out: today's time was used up",
    "time:schedule": "Signed out: the allowed hours ended",
    "time:login": "Refused a sign-in outside the allowed time",
}


def time_event_title(event: dict) -> str:
    return TIME_EVENT_TITLES.get(event.get("why", ""), "Signed out: time was up")


def today_sentence(counts: dict | None) -> str:
    """'14 blocked today' / '3 blocked, pictures hidden on 2 pages' / 'Nothing blocked today'."""
    if not counts:
        return "Nothing blocked today"
    parts = []
    if counts.get("blocked"):
        parts.append(f"{counts['blocked']} blocked")
    if counts.get("pictures"):
        parts.append(f"pictures hidden on {plural(counts['pictures'], 'page')}")
    if counts.get("searches"):
        parts.append(plural(counts["searches"], "search refused", "searches refused"))
    if not parts:
        return "Nothing blocked today"
    return ", ".join(parts) + " today"
