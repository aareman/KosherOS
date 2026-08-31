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
        youtube={"restrict": "strict", "blocked_categories": ["24", "20", "10"]},
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


def get(key: str) -> Profile:
    try:
        return BY_KEY[key]
    except KeyError:
        raise KeyError(f"unknown profile {key!r}") from None


def describe() -> list[dict]:
    """The profiles, for the admin app and the portal."""
    return [
        {"key": p.key, "label": p.label, "description": p.description,
         "mode": p.mode, "media_level": p.media_level,
         "language_filter": p.language_filter,
         "blocked_categories": list(p.blocked_categories),
         "youtube": dict(p.youtube), "can_install_apps": p.can_install_apps}
        for p in PROFILES
    ]


def _field(user, name, default=None):
    """Read a setting from a UserPolicy or from the dict form of one."""
    if isinstance(user, dict):
        return user.get(name, default)
    return getattr(user, name, default)


def matching(user) -> str | None:
    """The profile a user's settings correspond to, if any.

    Lets the admin app show "Teenager" instead of a page of switches, while
    still telling the truth when someone has customised beyond a profile.
    """
    for profile in PROFILES:
        if (_field(user, "mode") == profile.mode
                and sorted(_field(user, "blocked_categories", []) or [])
                    == sorted(profile.blocked_categories)
                and _field(user, "media_level", "none") == profile.media_level
                and _field(user, "language_filter", "off") == profile.language_filter
                and dict(_field(user, "youtube", {}) or {}) == dict(profile.youtube)
                and _field(user, "can_install_apps", True) == profile.can_install_apps):
            return profile.key
    return None
