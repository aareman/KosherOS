"""Policy store: load, validate, and atomically persist the device policy.

The policy file (/var/lib/kosher/policy.json) is the single contract shared by
kosherd, the admin app, and the future portal sync agent. Its shape is defined
by policy/schema/policy.schema.json in the source repo; a copy ships at
/usr/share/kosher/policy.schema.json.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path

import jsonschema

POLICY_PATH = Path("/var/lib/kosher/policy.json")
SCHEMA_PATH = Path("/usr/share/kosher/policy.schema.json")

# The ladder, from most restrictive to least:
#   none       no internet at all
#   whitelist  only named domains
#   dnsfilter  family DNS + forced safe search; the connection is not read
#   filtered   content filtering: safe search, category lists and page rules,
#              which requires reading the connection on this machine
#   unfiltered no filtering — an adult's own machine
MODES = ("none", "whitelist", "dnsfilter", "filtered", "unfiltered")

# Modes whose traffic is decrypted so content can be judged (see mitm/).
INSPECTED_MODES = ("filtered",)

# Modes that get safe search forced on them. dnsfilter cannot read the
# connection, so its safe search is done in DNS; filtered gets it in the
# proxy as well, which is stricter and covers more sites.
SAFESEARCH_MODES = ("dnsfilter", "filtered", "whitelist")

# Modes that must not reach the family resolver at all.
UNFILTERED_MODES = ("unfiltered",)

# How much of the imagery on an allowed page a user may see, strictest last.
# Only meaningful in filtered mode: judging a picture means holding it, which
# means reading the connection.
#   none           show everything
#   nsfw           hide explicit imagery
#   suggestive     also hide suggestive imagery
#   immodest       also hide immodest imagery (the frum default)
#   all            show no remote images at all
MEDIA_LEVELS = ("none", "nsfw", "suggestive", "immodest", "all")
MEDIA_LEVEL_LABELS = {
    "none": "Show all images",
    "nsfw": "Hide explicit images",
    "suggestive": "Hide explicit and suggestive images",
    "immodest": "Hide explicit, suggestive and immodest images",
    "all": "Hide all images from the web",
}
DEFAULT_MEDIA_LEVEL = "none"

# How a picture that is kept but partly hidden gets covered.
#   frost  the detected figure is frosted: shrunk to a few cells, smoothed
#          back, flattened toward its own colour — the shape is gone, the
#          page keeps its layout
#   skin   skin-toned pixels inside the detected figure are painted a solid
#          colour through an expanded mask; clothing and background stay.
#          Falls back to frost where the skin gate finds nothing to paint,
#          since a gate tuned on colour does not see every skin tone
COVER_STYLES = ("frost", "skin")
COVER_STYLE_LABELS = {
    "frost": "Frost the figure",
    "skin": "Paint over skin",
}
DEFAULT_COVER_STYLE = "frost"

# YouTube is enough of the web on its own to deserve its own settings.
YOUTUBE_CATEGORIES = {
    "1": "Film & Animation", "2": "Autos & Vehicles", "10": "Music",
    "15": "Pets & Animals", "17": "Sports", "19": "Travel & Events",
    "20": "Gaming", "22": "People & Blogs", "23": "Comedy",
    "24": "Entertainment", "25": "News & Politics", "26": "How-to & Style",
    "27": "Education", "28": "Science & Technology", "29": "Nonprofits",
}

# How the desktop is laid out for this account. A preference, not a
# protection: the filter is per-uid at the network layer and does not care
# which shell draws the windows. Applied at sign-in by kosher-layout.
#   classic   a taskbar along the bottom, a start button, minimise buttons:
#             what a Windows, Mac or ChromeOS user already knows (GNOME +
#             Dash to Panel)
#   tiling    GNOME with PaperWM's scrolling tiling, for keyboard-driven
#             power users; same lock screen, settings and accessibility
#   advanced  a separate niri + Noctalia session, chosen at the login screen;
#             configured by text files the user owns
LAYOUTS = ("classic", "tiling", "advanced")
LAYOUT_LABELS = {
    "classic": "Classic desktop",
    "tiling": "Tiling desktop",
    "advanced": "Advanced (niri)",
}
DEFAULT_LAYOUT = "classic"

# What earlier policies called these modes.
LEGACY_MODES = {"inspect": "filtered"}


def migrate(doc: dict) -> dict:
    """Bring an older policy document up to the current shape."""
    for user in doc.get("users", []):
        user["mode"] = LEGACY_MODES.get(user.get("mode"), user.get("mode"))
    guest = doc.get("guest")
    if isinstance(guest, dict) and "mode" in guest:
        guest["mode"] = LEGACY_MODES.get(guest["mode"], guest["mode"])
    return doc

# Reachable in whitelist mode for every user, so the OS itself keeps working:
# update registry, flatpak remote, GNOME connectivity check, NTP.
BUILTIN_SYSTEM_WHITELIST = (
    "nmcheck.gnome.org",
    "*.fedoraproject.org",
    "ghcr.io",
    "pkg-containers.githubusercontent.com",
    # Flathub: the source for approved app installs (the catalog decides
    # WHAT may be installed; this only lets the download reach the machine).
    "flathub.org",
    "dl.flathub.org",
    "*.cloudfront.net",
)

# The package registries developer tools fetch from, reachable from every
# account so that npm, pip, uv, gem, cargo, deno, bun and Go work on a
# filtered machine. Registries hold code, not pages: nothing here is a
# site a person browses, and every one of them is what a school or work
# project on this computer will need on day one. The tools trust the
# filter's certificate through /etc/profile.d/kosher-ca.sh.
DEVELOPER_REGISTRIES = (
    # Node: npm, yarn, pnpm, bun
    "registry.npmjs.org",
    "registry.yarnpkg.com",
    "bun.sh",
    "*.bun.sh",
    # Python: pip, uv
    "pypi.org",
    "files.pythonhosted.org",
    "astral.sh",
    "*.astral.sh",
    "github.com",                 # uv's Python builds and many tool installers are GitHub releases
    "objects.githubusercontent.com",
    "release-assets.githubusercontent.com",
    "raw.githubusercontent.com",
    # Ruby
    "rubygems.org",
    "*.rubygems.org",
    # Rust
    "crates.io",
    "*.crates.io",
    "static.rust-lang.org",
    # Deno / JSR
    "deno.land",
    "*.deno.land",
    "jsr.io",
    "*.jsr.io",
    # Go
    "proxy.golang.org",
    "sum.golang.org",
    "go.dev",
    "storage.googleapis.com",     # Go toolchain downloads
)


class PolicyError(Exception):
    """Raised for invalid policy documents or invalid mutations."""


@dataclass
class UserPolicy:
    uid: int
    username: str
    mode: str
    admin: bool = False
    whitelist: list[str] = field(default_factory=list)
    # Ready-made approved-site lists (whitelists.py) switched on for this
    # account, merged with `whitelist` when the filter is rendered. A
    # parent picks "Torah study" instead of typing thirty hosts.
    whitelist_bundles: list[str] = field(default_factory=list)
    apps: list[str] = field(default_factory=list)
    can_install_apps: bool = True
    rules: list[dict] = field(default_factory=list)
    blocked_categories: list[str] = field(default_factory=list)
    media_level: str = DEFAULT_MEDIA_LEVEL
    youtube: dict = field(default_factory=dict)
    language_filter: str = "off"
    layout: str = DEFAULT_LAYOUT
    cover_style: str = DEFAULT_COVER_STYLE
    # How long and when the account may be signed in (see timelimits.py).
    # Empty means no daily limit and always allowed — the default, so a
    # family that never opens the Time tab is not surprised by a sign-out.
    time: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        d: dict = {"uid": self.uid, "username": self.username, "mode": self.mode}
        if self.admin:
            d["admin"] = True
        if self.whitelist:
            d["whitelist"] = self.whitelist
        if self.whitelist_bundles:
            d["whitelist_bundles"] = self.whitelist_bundles
        if self.rules:
            d["rules"] = self.rules
        if self.blocked_categories:
            d["blocked_categories"] = self.blocked_categories
        if self.media_level != DEFAULT_MEDIA_LEVEL:
            d["media_level"] = self.media_level
        if self.youtube:
            d["youtube"] = self.youtube
        if self.language_filter != "off":
            d["language_filter"] = self.language_filter
        if self.apps:
            d["apps"] = self.apps
        if not self.can_install_apps:
            d["can_install_apps"] = False
        if self.layout != DEFAULT_LAYOUT:
            d["layout"] = self.layout
        if self.cover_style != DEFAULT_COVER_STYLE:
            d["cover_style"] = self.cover_style
        if self.time:
            d["time"] = self.time
        return d


GUEST_USERNAME = "kosher-guest"
# What a username may look like: what Fedora's useradd accepts, which
# includes capitals and dots. The rule used to insist on lowercase, and an
# admin who typed "Elisha" had the account created by accountsservice and
# then refused by the policy, leaving a half-made user nobody could manage.
USERNAME_PATTERN = r"^[A-Za-z_][A-Za-z0-9_.-]*$"
USERNAME_RULE = ("a username starts with a letter and uses only letters, "
                 "digits, dots, dashes and underscores")


@dataclass
class GuestPolicy:
    enabled: bool = False
    uid: int | None = None
    mode: str = "whitelist"
    whitelist: list[str] = field(default_factory=list)
    whitelist_bundles: list[str] = field(default_factory=list)
    rules: list[dict] = field(default_factory=list)
    blocked_categories: list[str] = field(default_factory=list)
    # The guest gets the same settings as anyone else. Without these a
    # guest in filtered mode had no picture filtering, no language
    # filtering and no YouTube limits whatever the household had chosen —
    # a hole in exactly the account nobody is watching.
    media_level: str = DEFAULT_MEDIA_LEVEL
    language_filter: str = "off"
    youtube: dict = field(default_factory=dict)
    cover_style: str = DEFAULT_COVER_STYLE
    time: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        d: dict = {"enabled": self.enabled}
        if self.uid is not None:
            d["uid"] = self.uid
        if self.mode != "whitelist":
            d["mode"] = self.mode
        if self.whitelist:
            d["whitelist"] = self.whitelist
        if self.whitelist_bundles:
            d["whitelist_bundles"] = self.whitelist_bundles
        if self.rules:
            d["rules"] = self.rules
        if self.blocked_categories:
            d["blocked_categories"] = self.blocked_categories
        if self.media_level != DEFAULT_MEDIA_LEVEL:
            d["media_level"] = self.media_level
        if self.language_filter != "off":
            d["language_filter"] = self.language_filter
        if self.youtube:
            d["youtube"] = self.youtube
        if self.cover_style != DEFAULT_COVER_STYLE:
            d["cover_style"] = self.cover_style
        if self.time:
            d["time"] = self.time
        return d


@dataclass
class Policy:
    revision: int = 0
    source: str = "local"
    users: list[UserPolicy] = field(default_factory=list)
    guardian_enabled: bool = False
    # Ads and trackers blocked at the resolver for EVERY account, like a
    # Pi-hole: the machine forces all DNS through its own resolvers, so this
    # reaches every browser and every app, not one browser's extension. On
    # by default; not content filtering, so it applies to unfiltered
    # accounts too, and turning it off is the guardian-gated act.
    adblock: bool = True
    system_whitelist: list[str] = field(default_factory=list)
    guest: GuestPolicy = field(default_factory=GuestPolicy)
    # A family's own presets, saved from a tuned account (see profiles.py).
    custom_profiles: list[dict] = field(default_factory=list)

    def user(self, uid: int) -> UserPolicy | None:
        return next((u for u in self.users if u.uid == uid), None)

    def account(self, uid: int):
        """A managed user OR the guest, by uid: whatever the filter settings
        apply to. The guest used to be reachable only through one coarse
        SetGuestConfig call and so could only be given a preset; every
        per-account filter setter now finds it here and changes it like
        anybody else. Account-management calls (admin, apps, removal) still
        use `user()` — the guest has none of those."""
        found = self.user(uid)
        if found is not None:
            return found
        if self.guest.enabled and self.guest.uid is not None and self.guest.uid == uid:
            return self.guest
        return None

    def effective_users(self) -> list[UserPolicy]:
        """Managed users plus the guest account (when enabled and created) —
        what enforcement (nftables, dnsmasq, malcontent) actually applies to."""
        users = list(self.users)
        if self.guest.enabled and self.guest.uid is not None:
            users.append(UserPolicy(
                uid=self.guest.uid, username=GUEST_USERNAME,
                mode=self.guest.mode, whitelist=list(self.guest.whitelist),
                whitelist_bundles=list(self.guest.whitelist_bundles),
                rules=list(self.guest.rules),
                blocked_categories=list(self.guest.blocked_categories),
                media_level=self.guest.media_level,
                language_filter=self.guest.language_filter,
                youtube=dict(self.guest.youtube),
                cover_style=self.guest.cover_style,
                time=dict(self.guest.time),
            ))
        return users

    def effective_system_whitelist(self) -> list[str]:
        return list(dict.fromkeys([*BUILTIN_SYSTEM_WHITELIST, *DEVELOPER_REGISTRIES,
                                   *self.system_whitelist]))

    def to_dict(self) -> dict:
        return {
            "schema_version": 1,
            "revision": self.revision,
            "source": self.source,
            "users": [u.to_dict() for u in self.users],
            "guardian": {"enabled": self.guardian_enabled},
            "adblock": {"enabled": self.adblock},
            **({"custom_profiles": list(self.custom_profiles)}
               if self.custom_profiles else {}),
            "system_whitelist": self.system_whitelist,
            "guest": self.guest.to_dict(),
        }

    @classmethod
    def from_dict(cls, doc: dict) -> "Policy":
        doc = migrate(doc)
        validate(doc)
        guest_doc = doc.get("guest", {"enabled": False})
        return cls(
            revision=doc["revision"],
            source=doc["source"],
            users=[
                UserPolicy(
                    uid=u["uid"],
                    username=u["username"],
                    mode=u["mode"],
                    admin=u.get("admin", False),
                    whitelist=list(u.get("whitelist", [])),
                    whitelist_bundles=list(u.get("whitelist_bundles", [])),
                    apps=list(u.get("apps", [])),
                    can_install_apps=u.get("can_install_apps", True),
                    rules=list(u.get("rules", [])),
                    blocked_categories=list(u.get("blocked_categories", [])),
                    media_level=u.get("media_level", DEFAULT_MEDIA_LEVEL),
                    youtube=dict(u.get("youtube", {})),
                    language_filter=u.get("language_filter", "off"),
                    layout=u.get("layout", DEFAULT_LAYOUT),
                    cover_style=u.get("cover_style", DEFAULT_COVER_STYLE),
                    time=dict(u.get("time", {})),
                )
                for u in doc["users"]
            ],
            guardian_enabled=doc["guardian"]["enabled"],
            adblock=bool(doc.get("adblock", {}).get("enabled", True)),
            custom_profiles=list(doc.get("custom_profiles") or []),
            system_whitelist=list(doc.get("system_whitelist", [])),
            guest=GuestPolicy(
                enabled=guest_doc["enabled"],
                uid=guest_doc.get("uid"),
                mode=guest_doc.get("mode", "whitelist"),
                whitelist=list(guest_doc.get("whitelist", [])),
                whitelist_bundles=list(guest_doc.get("whitelist_bundles", [])),
                rules=list(guest_doc.get("rules", [])),
                blocked_categories=list(guest_doc.get("blocked_categories", [])),
                media_level=guest_doc.get("media_level", DEFAULT_MEDIA_LEVEL),
                language_filter=guest_doc.get("language_filter", "off"),
                youtube=dict(guest_doc.get("youtube", {})),
                cover_style=guest_doc.get("cover_style", DEFAULT_COVER_STYLE),
                time=dict(guest_doc.get("time", {})),
            ),
        )


def _load_schema() -> dict:
    if SCHEMA_PATH.exists():
        return json.loads(SCHEMA_PATH.read_text())
    # Development fallback: schema bundled in the source tree next to the package.
    bundled = resources.files("kosherd").joinpath("data/policy.schema.json")
    return json.loads(bundled.read_text())


_schema_cache: dict | None = None


def validate(doc: dict) -> None:
    global _schema_cache
    if _schema_cache is None:
        _schema_cache = _load_schema()
    try:
        jsonschema.validate(doc, _schema_cache)
    except jsonschema.ValidationError as e:
        raise PolicyError(f"invalid policy: {e.message}") from e


def load(path: Path = POLICY_PATH) -> Policy:
    if not path.exists():
        return Policy()
    return Policy.from_dict(json.loads(path.read_text()))


def save(policy: Policy, path: Path = POLICY_PATH, *, bump_revision: bool = True) -> None:
    """Atomically write the policy (tmp + fsync + rename), root-only readable."""
    if bump_revision:
        policy.revision += 1
    doc = policy.to_dict()
    validate(doc)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".policy-")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(doc, f, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        os.rename(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise
