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
#   people         also hide any picture with a person in it, whatever
#                  they wear — tight or sheer clothing is not something a
#                  detector can judge, and this is the honest answer to it
MEDIA_LEVELS = ("none", "nsfw", "suggestive", "immodest", "people", "all")
MEDIA_LEVEL_LABELS = {
    "none": "Show all images",
    "nsfw": "Hide explicit images",
    "suggestive": "Hide explicit and suggestive images",
    "immodest": "Hide explicit, suggestive and immodest images",
    "people": "Hide immodest images and any picture of a person",
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
    # Not a YouTube category but a kind of video all the same: the
    # vertical endless feed, which no category limit reaches because a
    # short can be in any category. Blocking it refuses the /shorts pages,
    # the reel endpoints and the player when a short is what is playing,
    # and takes the Shorts shelves out of the feeds.
    "shorts": "Shorts",
}

# YouTube's payloads name a category ("Music", "Howto & Style") where the
# settings keep its id ("10", "26"); this reads either. Matched with the
# punctuation and case taken out, because YouTube writes "Howto" and the
# table writes "How-to".
_CATEGORY_KEY = {"".join(ch for ch in label.lower() if ch.isalnum()): code
                 for code, label in YOUTUBE_CATEGORIES.items()}


def youtube_category_id(value: str | None) -> str | None:
    """A category id from an id or a name, or None for neither."""
    if not value:
        return None
    text = str(value).strip()
    if text in YOUTUBE_CATEGORIES:
        return text
    return _CATEGORY_KEY.get("".join(ch for ch in text.lower() if ch.isalnum()))

# How the desktop is laid out for this account. A preference, not a
# protection: the filter is per-uid at the network layer and does not care
# which shell draws the windows. Applied at sign-in by kosher-layout.
#   classic   a taskbar along the bottom, a start button, minimise buttons:
#             what a Windows, Mac or ChromeOS user already knows (GNOME +
#             Dash to Panel)
#   tiling    GNOME with PaperWM's scrolling tiling, for keyboard-driven
#             power users; same lock screen, settings and accessibility
#   advanced  the Noctalia shell in a separate login-screen session, on the
#             compositor the person picks (COMPOSITORS); configured by text
#             files the user owns
LAYOUTS = ("classic", "tiling", "advanced")
LAYOUT_LABELS = {
    "classic": "Classic desktop",
    "tiling": "Tiling desktop",
    "advanced": "Advanced (Noctalia)",
}
DEFAULT_LAYOUT = "classic"

# Which compositor the advanced layout runs Noctalia on. Each is its own
# session on the login screen, named by its wayland-sessions file:
#   niri      windows in columns on a strip that scrolls sideways
#   hyprland  dynamic tiling: each new window splits the space
#   umbriel   Noctalia's own compositor, scrolling or tiled
# Only read when the layout is advanced; kept when it is not, so going back
# to advanced finds the compositor the person had.
COMPOSITORS = ("niri", "hyprland", "umbriel")
DEFAULT_COMPOSITOR = "niri"

# How often the computer fetches a new KosherOS version on its own (#19).
# The download is staged for the next restart; nothing restarts by itself.
#   off       only when an administrator presses Check
#   daily … monthly   that often, counted from the last check that worked
# Weekly by default: a family gets fixes within a week, and a notice at
# most once a week is not nagging (see updates.py).
AUTO_UPDATE_SCHEDULES = ("off", "daily", "weekly", "biweekly", "monthly")
DEFAULT_AUTO_UPDATES = "weekly"

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
    # Nix, devenv, devbox. The daemon's own downloads (the binary cache) run
    # as root and are not filtered; these are what the account's side of a
    # build reaches: the flake registry, nixpkgs as a tarball from GitHub,
    # and devbox's package search.
    "cache.nixos.org",
    "channels.nixos.org",         # the flake registry (nixpkgs = github:NixOS/nixpkgs)
    "api.github.com",             # a flake's branch -> commit lookup
    "codeload.github.com",        # where github.com sends an archive download
    "devenv.cachix.org",
    "search.devbox.sh",
    # Where the AI tools and editors the Store carries from nixpkgs are
    # published (the unfree ones are not on the binary cache, so a build
    # fetches the release itself): Claude Code, Cursor, Antigravity. The
    # rest are GitHub releases, already above.
    "downloads.claude.ai",
    "downloads.cursor.com",
    "edgedl.me.gvt1.com",         # Google's download host (Antigravity)
    # Container image registries, for rootless podman: Docker Hub (not
    # hub.docker.com, which is a site), Quay, Microsoft's (every
    # devcontainer image), Red Hat's; Fedora's and GitHub's are above.
    "docker.io",
    "registry-1.docker.io",
    "auth.docker.io",
    "index.docker.io",
    "production.cloudflare.docker.com",
    "quay.io",
    "*.quay.io",
    "mcr.microsoft.com",
    "*.data.mcr.microsoft.com",
    "registry.access.redhat.com",
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
    # Which installed apps this account may run, as an allow-list. The
    # older shape of app control; kosherd turns it into `blocked_apps` on
    # start-up once it can see what is installed, and malcontent honours
    # it until then. New policies leave it empty.
    apps: list[str] = field(default_factory=list)
    can_install_apps: bool = True
    # What the Store offers this account (appaccess.py): "approved" for the
    # approved list only, "store" for the whole store minus what is
    # blocked. None means the default for the kind of account — the
    # approved list, or the whole store for an administrator.
    app_access: str | None = None
    # Kinds of app (appkinds.KIND_KEYS) and single apps this account may
    # neither install nor run, in either access mode.
    blocked_app_kinds: list[str] = field(default_factory=list)
    blocked_apps: list[str] = field(default_factory=list)
    rules: list[dict] = field(default_factory=list)
    blocked_categories: list[str] = field(default_factory=list)
    media_level: str = DEFAULT_MEDIA_LEVEL
    youtube: dict = field(default_factory=dict)
    # Calmer versions of the addictive sites (calm.py, #179): feeds end
    # after a set number of items, a linked post renders on its own, and
    # a site may have a daily time budget. Empty means off.
    calm: dict = field(default_factory=dict)
    language_filter: str = "off"
    layout: str = DEFAULT_LAYOUT
    compositor: str = DEFAULT_COMPOSITOR
    cover_style: str = DEFAULT_COVER_STYLE
    # How long and when the account may be signed in (see timelimits.py).
    # Empty means no daily limit and always allowed — the default, so a
    # family that never opens the Time tab is not surprised by a sign-out.
    time: dict = field(default_factory=dict)
    # Beyond the web (see nft.py). UDP cannot be inspected, so whether this
    # account may send it to high ports — what a video call needs — is a
    # decision, on by default so school calls work unconfigured. Extra TCP
    # ports are the advanced escape hatch for a program that is neither
    # web nor mail; everything not named is refused.
    video_calls: bool = True
    extra_ports: list[int] = field(default_factory=list)
    # Developer tools (#123): may this account use the Nix daemon itself —
    # nix, devbox, devenv from a shell — and reach the package registries
    # directly? Off by default: the Store still installs catalogue apps for
    # everyone through kosherd, and an account that only browses never
    # needs a build daemon that fetches as a system user.
    developer_tools: bool = False
    # Apps an administrator approved for this account alone (#213). Most
    # apps are approved for the whole computer in catalog.json; the few in
    # appaccess.INDIVIDUAL_APPROVAL (Syncthing) need it per account on a
    # supervised one, whatever its app access, because what they bring in
    # never passes the filter.
    approved_apps: list[str] = field(default_factory=list)
    # May devices on the home network connect to this account's Syncthing
    # directly (syncthing.py)? Off by default: with it off, Syncthing only
    # dials out, and nothing on the LAN reaches in.
    syncthing_lan: bool = False
    # The group this account is in (a key in Policy.custom_profiles), or
    # None. Explicit, never inferred from matching settings: an account may
    # differ from its group and still belong to it, and a change to the
    # group reaches it (see profiles.py).
    profile: str | None = None

    def to_dict(self) -> dict:
        d: dict = {"uid": self.uid, "username": self.username, "mode": self.mode}
        if self.profile:
            d["profile"] = self.profile
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
        if self.calm:
            d["calm"] = self.calm
        if self.language_filter != "off":
            d["language_filter"] = self.language_filter
        if self.apps:
            d["apps"] = self.apps
        if not self.can_install_apps:
            d["can_install_apps"] = False
        if self.app_access:
            d["app_access"] = self.app_access
        if self.blocked_app_kinds:
            d["blocked_app_kinds"] = self.blocked_app_kinds
        if self.blocked_apps:
            d["blocked_apps"] = self.blocked_apps
        if self.layout != DEFAULT_LAYOUT:
            d["layout"] = self.layout
        if self.compositor != DEFAULT_COMPOSITOR:
            d["compositor"] = self.compositor
        if self.cover_style != DEFAULT_COVER_STYLE:
            d["cover_style"] = self.cover_style
        if self.time:
            d["time"] = self.time
        if not self.video_calls:
            d["video_calls"] = False
        if self.extra_ports:
            d["extra_ports"] = sorted(set(int(p) for p in self.extra_ports))
        if self.developer_tools:
            d["developer_tools"] = True
        if self.approved_apps:
            d["approved_apps"] = sorted(set(self.approved_apps))
        if self.syncthing_lan:
            d["syncthing_lan"] = True
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
    calm: dict = field(default_factory=dict)
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
        if self.calm:
            d["calm"] = self.calm
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
    # Automatic updates (#19): the schedule, and whether the people signed
    # in are told once when a downloaded update waits for a restart.
    auto_updates: str = DEFAULT_AUTO_UPDATES
    update_notify: bool = True
    # Features the administrator turns on for the whole computer (#220),
    # off on a new install. None until kosherd's start decides for a
    # computer that had them before there was a switch (daemon.py).
    tailscale: bool | None = None
    syncthing: bool | None = None
    system_whitelist: list[str] = field(default_factory=list)
    guest: GuestPolicy = field(default_factory=GuestPolicy)
    # A family's own presets, saved from a tuned account (see profiles.py).
    custom_profiles: list[dict] = field(default_factory=list)
    # Hosts an administrator added to where a Nix build may download from
    # (#199); the built-in list is buildsources.DEFAULT_BUILD_SOURCES.
    build_sources_extra: list[str] = field(default_factory=list)

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
                # The guest installs nothing and is wiped at sign-out;
                # what it may run is the approved list, like any account
                # in no group.
                can_install_apps=False,
                rules=list(self.guest.rules),
                blocked_categories=list(self.guest.blocked_categories),
                media_level=self.guest.media_level,
                language_filter=self.guest.language_filter,
                youtube=dict(self.guest.youtube),
                calm=dict(self.guest.calm),
                cover_style=self.guest.cover_style,
                time=dict(self.guest.time),
            ))
        return users

    def effective_system_whitelist(self) -> list[str]:
        """What every account may reach on 80/443 without the proxy: the
        machine's own needs. The developer registries are NOT here (#123):
        they went in once for every account and made "whitelist only" mean
        the family's list plus GitHub; a developer-flagged account reaches
        them through its own mode (whitelists.effective adds them)."""
        return list(dict.fromkeys([*BUILTIN_SYSTEM_WHITELIST, *self.system_whitelist]))

    def switched_off_apps(self) -> frozenset[str]:
        """Store apps this computer does not offer while their switch is off."""
        return frozenset() if self.syncthing else frozenset({"nixpkgs#syncthing"})

    def developer_accounts(self) -> list["UserPolicy"]:
        return [u for u in self.effective_users() if u.developer_tools and u.mode != "none"]

    def build_sources(self) -> list[str]:
        """Every host a Nix build may download from: the built-in list and
        the administrator's additions (#199)."""
        from . import buildsources

        return buildsources.combined(self.build_sources_extra)

    def advanced(self) -> dict:
        """The switches that have been decided, as the policy file keeps them."""
        return {name: on for name, on in (("tailscale", self.tailscale),
                                          ("syncthing", self.syncthing)) if on is not None}

    def to_dict(self) -> dict:
        return {
            "schema_version": 1,
            "revision": self.revision,
            "source": self.source,
            "users": [u.to_dict() for u in self.users],
            "guardian": {"enabled": self.guardian_enabled},
            "adblock": {"enabled": self.adblock},
            "updates": {"auto": self.auto_updates, "notify": self.update_notify},
            **({"advanced": self.advanced()} if self.advanced() else {}),
            **({"custom_profiles": list(self.custom_profiles)}
               if self.custom_profiles else {}),
            "system_whitelist": self.system_whitelist,
            **({"developer": {"build_sources_extra": list(self.build_sources_extra)}}
               if self.build_sources_extra else {}),
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
                    app_access=u.get("app_access") or None,
                    blocked_app_kinds=list(u.get("blocked_app_kinds", [])),
                    blocked_apps=list(u.get("blocked_apps", [])),
                    rules=list(u.get("rules", [])),
                    blocked_categories=list(u.get("blocked_categories", [])),
                    media_level=u.get("media_level", DEFAULT_MEDIA_LEVEL),
                    youtube=dict(u.get("youtube", {})),
                    calm=dict(u.get("calm", {})),
                    language_filter=u.get("language_filter", "off"),
                    layout=u.get("layout", DEFAULT_LAYOUT),
                    compositor=u.get("compositor", DEFAULT_COMPOSITOR),
                    cover_style=u.get("cover_style", DEFAULT_COVER_STYLE),
                    time=dict(u.get("time", {})),
                    video_calls=bool(u.get("video_calls", True)),
                    extra_ports=[int(p) for p in u.get("extra_ports", [])],
                    developer_tools=bool(u.get("developer_tools", False)),
                    approved_apps=list(u.get("approved_apps", [])),
                    syncthing_lan=bool(u.get("syncthing_lan", False)),
                    profile=u.get("profile") or None,
                )
                for u in doc["users"]
            ],
            guardian_enabled=doc["guardian"]["enabled"],
            adblock=bool(doc.get("adblock", {}).get("enabled", True)),
            auto_updates=(doc.get("updates") or {}).get("auto", DEFAULT_AUTO_UPDATES),
            update_notify=bool((doc.get("updates") or {}).get("notify", True)),
            tailscale=(doc.get("advanced") or {}).get("tailscale"),
            syncthing=(doc.get("advanced") or {}).get("syncthing"),
            custom_profiles=list(doc.get("custom_profiles") or []),
            system_whitelist=list(doc.get("system_whitelist", [])),
            build_sources_extra=list((doc.get("developer") or {}).get("build_sources_extra", [])),
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
                calm=dict(guest_doc.get("calm", {})),
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
