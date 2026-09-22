"""Groups: settings a family names once and gives to several accounts.

The settings underneath — filter mode, categories, media level, YouTube
limits, language filtering, app installs — are each defensible on their
own and together are a wall of switches. So they travel together: an
admin tunes one account until it is right, saves it as a group ("Kids",
"Yeshiva bochurim"), and puts the other accounts in it. Change the group
and every account in it changes with it.

Nothing ready-made ships. Labels like Child or Teenager presume a
family's judgement, and the same word means different things in
different homes — in the user's words, "they are just labels that don't
match well and can be changed to mean many different things to different
people". What ships instead is a complete, strict default for an account
that is in no group (DEFAULTS), and sensible content settings for each
kind of internet (MODE_DEFAULTS), so nothing is ever half set up.

The module keeps its old name and the storage keeps its old field
(`custom_profiles`, keys prefixed "custom-") so policies written when
groups were "custom presets" still load.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .categories import DEFAULT_BLOCKED


@dataclass(frozen=True)
class Profile:
    """One group's settings. Also the shape of the defaults."""

    key: str
    label: str
    description: str
    mode: str
    blocked_categories: tuple[str, ...] = ()
    media_level: str = "none"
    language_filter: str = "off"
    youtube: dict = field(default_factory=dict)
    can_install_apps: bool = True
    # What the Store offers (appaccess.py) and what is blocked from it.
    app_access: str = "approved"
    blocked_app_kinds: tuple[str, ...] = ()
    blocked_apps: tuple[str, ...] = ()


# What an account gets when it is in no group: the open web with content
# filtering, strict enough to be right for a family that never changes it.
DEFAULTS = Profile(
    key="", label="", description=(
        "The open web with content filtering: adult, gambling, dating, "
        "social media and video are blocked, images are checked, and bad "
        "language is cleaned up."),
    mode="filtered",
    blocked_categories=tuple(sorted({*DEFAULT_BLOCKED, "social", "video",
                                     "immodest", "violence", "drugs"})),
    media_level="immodest",
    language_filter="substitute",
    youtube={"restrict": "strict", "blocked_categories": ["24", "20", "10", "shorts"]},
    can_install_apps=False,
)

# What an administrator's own account gets: the floor — adult content,
# gambling, dating and filter bypasses blocked — with the rest open. The
# parent is not who the strict defaults are for; they can put themselves
# in a group like anyone else.
ADMIN_DEFAULTS = Profile(
    key="", label="", description="Filtering for an adult: the floor, and the rest open.",
    mode="filtered", blocked_categories=tuple(sorted(DEFAULT_BLOCKED)),
    media_level="none", language_filter="off", youtube={}, can_install_apps=True,
    app_access="store")

# The content settings that go with each kind of internet, for an account
# or the guest set up by the kind alone. Not groups, and not named: an
# admin who chooses "Approved sites only" still gets pictures, language
# and YouTube handled the way that kind of account needs.
MODE_DEFAULTS = {
    "none": Profile(
        key="", label="", description="No internet at all.",
        mode="none", blocked_categories=tuple(sorted(DEFAULT_BLOCKED)),
        media_level="all", language_filter="substitute",
        youtube={"restrict": "strict"}, can_install_apps=False),
    "whitelist": Profile(
        key="", label="", description=(
            "A short list of approved sites and nothing else. No images from "
            "the wider web, no YouTube, and apps chosen by you."),
        mode="whitelist",
        blocked_categories=tuple(sorted({*DEFAULT_BLOCKED, "social", "video",
                                         "games", "shopping", "ads"})),
        media_level="all", language_filter="substitute",
        youtube={"restrict": "strict", "allowed_channels": []},
        can_install_apps=False),
    "filtered": DEFAULTS,
    "dnsfilter": Profile(
        key="", label="", description=(
            "Blocks known bad sites and forces safe search, without reading "
            "this person's connections. Weaker, but nothing is decrypted."),
        mode="dnsfilter", blocked_categories=tuple(sorted(DEFAULT_BLOCKED)),
        media_level="none", language_filter="off",
        youtube={"restrict": "moderate"}, can_install_apps=True),
    "unfiltered": Profile(
        key="", label="", description="No filtering of any kind for this account.",
        mode="unfiltered", can_install_apps=True),
}


def for_mode(mode: str) -> Profile:
    """The complete settings for an account set up by its kind of internet."""
    return MODE_DEFAULTS[mode]


# No groups ship. Kept as a name so a caller asking for "the built-ins"
# gets the truthful answer rather than an import error.
PROFILES: tuple[Profile, ...] = ()

# Group keys carry this prefix from when groups were "custom presets"
# beside built-in ones; the policies on disk still use it.
CUSTOM_PREFIX = "custom-"

# The settings a group fixes, in the order a parent reads them.
DIFF_FIELDS = ("blocked_categories", "media_level", "language_filter",
               "youtube", "can_install_apps", "app_access", "blocked_app_kinds",
               "blocked_apps")


def slug(label: str) -> str:
    """'Yeshiva bochur' -> 'custom-yeshiva-bochur'."""
    body = re.sub(r"[^a-z0-9]+", "-", label.strip().lower()).strip("-")
    return CUSTOM_PREFIX + (body or "group")


def to_dict(profile: Profile) -> dict:
    return {"key": profile.key, "label": profile.label,
            "description": profile.description, "mode": profile.mode,
            "blocked_categories": list(profile.blocked_categories),
            "media_level": profile.media_level,
            "language_filter": profile.language_filter,
            "youtube": dict(profile.youtube),
            "can_install_apps": profile.can_install_apps,
            "app_access": profile.app_access,
            "blocked_app_kinds": list(profile.blocked_app_kinds),
            "blocked_apps": list(profile.blocked_apps)}


def from_dict(doc: dict) -> Profile:
    return Profile(
        key=str(doc["key"]), label=str(doc.get("label") or doc["key"]),
        description=str(doc.get("description") or ""),
        mode=str(doc["mode"]),
        blocked_categories=tuple(sorted(doc.get("blocked_categories") or [])),
        media_level=str(doc.get("media_level") or "none"),
        language_filter=str(doc.get("language_filter") or "off"),
        youtube=dict(doc.get("youtube") or {}),
        can_install_apps=bool(doc.get("can_install_apps", True)),
        app_access=str(doc.get("app_access") or "approved"),
        blocked_app_kinds=tuple(sorted(doc.get("blocked_app_kinds") or [])),
        blocked_apps=tuple(sorted(doc.get("blocked_apps") or [])))


def _field(user, name, default=None):
    """Read a setting from a UserPolicy or from the dict form of one."""
    if isinstance(user, dict):
        return user.get(name, default)
    return getattr(user, name, default)


def from_user(user, label: str, description: str = "") -> Profile:
    """Snapshot an account's current settings as a group.

    The everyday path to a good group: a parent tunes one account until it
    is right, saves it, and puts the others in it.
    """
    return Profile(
        key=slug(label), label=label.strip(), description=description.strip(),
        mode=str(_field(user, "mode")),
        blocked_categories=tuple(sorted(_field(user, "blocked_categories", []) or [])),
        media_level=str(_field(user, "media_level", "none") or "none"),
        language_filter=str(_field(user, "language_filter", "off") or "off"),
        youtube=dict(_field(user, "youtube", {}) or {}),
        can_install_apps=bool(_field(user, "can_install_apps", True)),
        app_access=_access_of(user),
        blocked_app_kinds=tuple(sorted(_field(user, "blocked_app_kinds", []) or [])),
        blocked_apps=tuple(sorted(_field(user, "blocked_apps", []) or [])))


def _access_of(user) -> str:
    """An account's app access with its default filled in (appaccess.py)."""
    from .appaccess import access_of

    return access_of(user)


# The app settings a group carries. The guest has none of these fields:
# it installs nothing and runs the approved list, which needs no setting.
_APP_FIELDS = ("can_install_apps", "app_access", "blocked_app_kinds", "blocked_apps")


def apply(user, profile: Profile) -> None:
    """Give an account (a UserPolicy, a GuestPolicy or the dict form of one)
    a group's settings. Membership is the caller's to set: this is also how
    the defaults reach a new account, and the defaults are no group."""
    values = {"mode": profile.mode,
              "blocked_categories": list(profile.blocked_categories),
              "media_level": profile.media_level,
              "language_filter": profile.language_filter,
              "youtube": dict(profile.youtube)}
    app_values = {"can_install_apps": profile.can_install_apps,
                  "app_access": profile.app_access,
                  "blocked_app_kinds": list(profile.blocked_app_kinds),
                  "blocked_apps": list(profile.blocked_apps)}
    if isinstance(user, dict):
        user.update(values)
        if "can_install_apps" in user:  # the guest installs nothing
            user.update(app_values)
        return
    for name, value in values.items():
        setattr(user, name, value)
    if hasattr(user, "can_install_apps"):
        for name, value in app_values.items():
            setattr(user, name, value)


def all_profiles(custom=()) -> tuple[Profile, ...]:
    """The family's groups, in the order they were made."""
    return tuple(c if isinstance(c, Profile) else from_dict(c) for c in (custom or ()))


def get(key: str, custom=()) -> Profile:
    for profile in all_profiles(custom):
        if profile.key == key:
            return profile
    raise KeyError(f"unknown group {key!r}")


def describe(custom=()) -> list[dict]:
    """The groups, for the admin app and the portal."""
    return [{**to_dict(p), "custom": True} for p in all_profiles(custom)]


def members(users, key: str) -> list:
    """The accounts in a group."""
    return [u for u in users if _field(u, "profile") == key]


def diff(user, custom=()) -> tuple[str | None, list[dict]]:
    """The group an account is in, and every way it departs from it.

    Membership is explicit (`profile` on the account), never guessed from
    the settings: an account one switch away from its group is still in
    it, and that one switch is what this reports — "Kids, with 1 change:
    Sports also blocked" — so the admin app can offer to put it back, or
    to give the whole group the change. An account in no group, or whose
    group has been deleted, reports (None, []).

    Each change is a dict the app turns into a sentence: {"field": ...,
    "from": ..., "to": ...}, or for categories {"field":
    "blocked_categories", "added": [...], "removed": [...]}.
    """
    key = _field(user, "profile") or None
    if not key:
        return None, []
    try:
        profile = get(key, custom)
    except KeyError:
        return None, []
    return key, _changes(user, profile)


def _changes(user, profile: Profile) -> list[dict]:
    changes: list[dict] = []
    have = set(_field(user, "blocked_categories", []) or [])
    want = set(profile.blocked_categories)
    if have != want:
        changes.append({"field": "blocked_categories",
                        "added": sorted(have - want),
                        "removed": sorted(want - have)})
    if _field(user, "mode") != profile.mode:
        changes.append({"field": "mode", "from": profile.mode, "to": _field(user, "mode")})
    for field_name, default in (("media_level", "none"),
                                ("language_filter", "off"),
                                ("can_install_apps", True)):
        theirs = _field(user, field_name, default)
        if theirs is None:
            theirs = default
        ours = getattr(profile, field_name)
        if theirs != ours:
            changes.append({"field": field_name, "from": ours, "to": theirs})
    theirs = dict(_field(user, "youtube", {}) or {})
    if theirs != dict(profile.youtube):
        changes.append({"field": "youtube", "from": dict(profile.youtube),
                        "to": theirs})
    if _access_of(user) != profile.app_access:
        changes.append({"field": "app_access", "from": profile.app_access,
                        "to": _access_of(user)})
    for field_name in ("blocked_app_kinds", "blocked_apps"):
        have = set(_field(user, field_name, []) or [])
        want = set(getattr(profile, field_name))
        if have != want:
            changes.append({"field": field_name,
                            "added": sorted(have - want),
                            "removed": sorted(want - have)})
    return changes
