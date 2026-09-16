"""Ready-made profiles, so a parent makes one choice per child.

The settings underneath — filter mode, categories, media level, YouTube
limits, language filtering, app installs — are each defensible on their
own and together are a wall of switches. A parent setting up a family
computer has neither the time nor the background to reason about twenty
toggles, and a filter that demands that gets configured once, badly, or
not at all.

So the product asks one question: who is this account for? Everything
below follows from the answer, and any of it can still be changed
afterwards by an admin who wants to.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .categories import DEFAULT_BLOCKED


@dataclass(frozen=True)
class Profile:
    key: str
    label: str
    description: str
    mode: str
    blocked_categories: tuple[str, ...] = ()
    media_level: str = "none"
    language_filter: str = "off"
    youtube: dict = field(default_factory=dict)
    can_install_apps: bool = True


# Ordered from most protected to least; the admin app shows them in this
# order because that is the order a parent thinks in.
PROFILES = (
    Profile(
        key="young_child",
        label="Young child",
        description=(
            "A short list of approved sites and nothing else. No images from "
            "the wider web, no YouTube, and apps chosen by you."),
        mode="whitelist",
        blocked_categories=tuple(sorted({*DEFAULT_BLOCKED, "social", "video",
                                         "games", "shopping", "ads"})),
        media_level="all",
        language_filter="substitute",
        youtube={"restrict": "strict", "allowed_channels": []},
        can_install_apps=False,
    ),
    Profile(
        key="child",
        label="Child",
        description=(
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
    ),
    Profile(
        key="teen",
        label="Teenager",
        description=(
            "Content filtering with more room: adult and gambling stay "
            "blocked and immodest images are hidden, but ordinary sites, "
            "news and approved video are allowed."),
        mode="filtered",
        blocked_categories=tuple(sorted({*DEFAULT_BLOCKED, "immodest",
                                         "violence", "drugs"})),
        media_level="immodest",
        language_filter="substitute",
        youtube={"restrict": "moderate"},
        can_install_apps=True,
    ),
    Profile(
        key="adult",
        label="Adult",
        description=(
            "Filtering for an adult who wants it: adult content and filter "
            "bypasses are blocked, immodest images are hidden, everything "
            "else is open."),
        mode="filtered",
        blocked_categories=tuple(sorted({*DEFAULT_BLOCKED, "immodest"})),
        media_level="immodest",
        language_filter="off",
        youtube={"restrict": "moderate"},
        can_install_apps=True,
    ),
    Profile(
        key="dns_only",
        label="Basic protection",
        description=(
            "Blocks known bad sites and forces safe search, without reading "
            "this person's connections. Weaker, but nothing is decrypted."),
        mode="dnsfilter",
        blocked_categories=tuple(sorted(DEFAULT_BLOCKED)),
        media_level="none",
        language_filter="off",
        youtube={"restrict": "moderate"},
        can_install_apps=True,
    ),
    Profile(
        key="unfiltered",
        label="No filtering",
        description="No filtering of any kind for this account.",
        mode="unfiltered",
        can_install_apps=True,
    ),
)

BY_KEY = {p.key: p for p in PROFILES}
DEFAULT_PROFILE = "child"

# Custom presets — a family's own, saved from an account they have tuned —
# carry this prefix so they can never collide with a built-in key.
CUSTOM_PREFIX = "custom-"


def slug(label: str) -> str:
    """'Yeshiva bochur' -> 'custom-yeshiva-bochur'."""
    body = re.sub(r"[^a-z0-9]+", "-", label.strip().lower()).strip("-")
    return CUSTOM_PREFIX + (body or "preset")


def to_dict(profile: Profile) -> dict:
    return {"key": profile.key, "label": profile.label,
            "description": profile.description, "mode": profile.mode,
            "blocked_categories": list(profile.blocked_categories),
            "media_level": profile.media_level,
            "language_filter": profile.language_filter,
            "youtube": dict(profile.youtube),
            "can_install_apps": profile.can_install_apps}


def from_dict(doc: dict) -> Profile:
    return Profile(
        key=str(doc["key"]), label=str(doc.get("label") or doc["key"]),
        description=str(doc.get("description") or ""),
        mode=str(doc["mode"]),
        blocked_categories=tuple(sorted(doc.get("blocked_categories") or [])),
        media_level=str(doc.get("media_level") or "none"),
        language_filter=str(doc.get("language_filter") or "off"),
        youtube=dict(doc.get("youtube") or {}),
        can_install_apps=bool(doc.get("can_install_apps", True)))


def from_user(user, label: str, description: str = "") -> Profile:
    """Snapshot an account's current settings as a preset.

    The everyday path to a good preset: a parent tunes one child's account
    until it is right, then saves it and applies it to the others.
    """
    return Profile(
        key=slug(label), label=label.strip(), description=description.strip(),
        mode=str(_field(user, "mode")),
        blocked_categories=tuple(sorted(_field(user, "blocked_categories", []) or [])),
        media_level=str(_field(user, "media_level", "none") or "none"),
        language_filter=str(_field(user, "language_filter", "off") or "off"),
        youtube=dict(_field(user, "youtube", {}) or {}),
        can_install_apps=bool(_field(user, "can_install_apps", True)))


def all_profiles(custom=()) -> tuple[Profile, ...]:
    """Built-ins first, in their order, then the family's own presets."""
    return PROFILES + tuple(c if isinstance(c, Profile) else from_dict(c)
                            for c in (custom or ()))


def get(key: str, custom=()) -> Profile:
    for profile in all_profiles(custom):
        if profile.key == key:
            return profile
    raise KeyError(f"unknown profile {key!r}")


def describe(custom=()) -> list[dict]:
    """The profiles, for the admin app and the portal."""
    return [{**to_dict(p), "custom": p.key.startswith(CUSTOM_PREFIX)}
            for p in all_profiles(custom)]


def _field(user, name, default=None):
    """Read a setting from a UserPolicy or from the dict form of one."""
    if isinstance(user, dict):
        return user.get(name, default)
    return getattr(user, name, default)


def matching(user, custom=()) -> str | None:
    """The profile a user's settings correspond to, if any.

    Lets the admin app show "Teenager" instead of a page of switches, while
    still telling the truth when someone has customised beyond a profile.
    """
    for profile in all_profiles(custom):
        if (_field(user, "mode") == profile.mode
                and sorted(_field(user, "blocked_categories", []) or [])
                    == sorted(profile.blocked_categories)
                and _field(user, "media_level", "none") == profile.media_level
                and _field(user, "language_filter", "off") == profile.language_filter
                and dict(_field(user, "youtube", {}) or {}) == dict(profile.youtube)
                and _field(user, "can_install_apps", True) == profile.can_install_apps):
            return profile.key
    return None


# The settings a preset fixes, in the order a parent reads them.
DIFF_FIELDS = ("blocked_categories", "media_level", "language_filter",
               "youtube", "can_install_apps")


def diff(user, custom=()) -> tuple[str | None, list[dict]]:
    """The preset an account is closest to, and every way it departs.

    `matching` says "Teenager" or nothing. Nothing is the truthful answer
    and a useless one: an account one switch away from Teenager reads as
    "Custom", and a parent cannot tell whether that is one deliberate
    change or a stranger's configuration. This says "Teenager, with 1
    change: Sports also blocked", and the admin app offers to put it back.

    The nearest preset is the one in the same filter mode with the fewest
    differing settings; ties go to the more protective (earlier) preset.
    An account in a mode no preset uses has no nearest preset. Each change
    is a dict the app turns into a sentence: {"field": ..., "from": ...,
    "to": ...}, or for categories {"field": "blocked_categories",
    "added": [...], "removed": [...]}.
    """
    mode = _field(user, "mode")
    best: tuple[int, str, list[dict]] | None = None
    for profile in all_profiles(custom):
        if profile.mode != mode:
            continue
        changes = _changes(user, profile)
        if best is None or len(changes) < best[0]:
            best = (len(changes), profile.key, changes)
        if not changes:
            break
    if best is None:
        return None, []
    return best[1], best[2]


def _changes(user, profile: Profile) -> list[dict]:
    changes: list[dict] = []
    have = set(_field(user, "blocked_categories", []) or [])
    want = set(profile.blocked_categories)
    if have != want:
        changes.append({"field": "blocked_categories",
                        "added": sorted(have - want),
                        "removed": sorted(want - have)})
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
    return changes

