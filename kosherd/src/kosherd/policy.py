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

# YouTube is enough of the web on its own to deserve its own settings.
YOUTUBE_CATEGORIES = {
    "1": "Film & Animation", "2": "Autos & Vehicles", "10": "Music",
    "15": "Pets & Animals", "17": "Sports", "19": "Travel & Events",
    "20": "Gaming", "22": "People & Blogs", "23": "Comedy",
    "24": "Entertainment", "25": "News & Politics", "26": "How-to & Style",
    "27": "Education", "28": "Science & Technology", "29": "Nonprofits",
}

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


class PolicyError(Exception):
    """Raised for invalid policy documents or invalid mutations."""


@dataclass
class UserPolicy:
    uid: int
    username: str
    mode: str
    admin: bool = False
    whitelist: list[str] = field(default_factory=list)
    apps: list[str] = field(default_factory=list)
    can_install_apps: bool = True
    rules: list[dict] = field(default_factory=list)
    blocked_categories: list[str] = field(default_factory=list)
    media_level: str = DEFAULT_MEDIA_LEVEL
    youtube: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        d: dict = {"uid": self.uid, "username": self.username, "mode": self.mode}
        if self.admin:
            d["admin"] = True
        if self.whitelist:
            d["whitelist"] = self.whitelist
        if self.rules:
            d["rules"] = self.rules
        if self.blocked_categories:
            d["blocked_categories"] = self.blocked_categories
        if self.media_level != DEFAULT_MEDIA_LEVEL:
            d["media_level"] = self.media_level
        if self.youtube:
            d["youtube"] = self.youtube
        if self.apps:
            d["apps"] = self.apps
        if not self.can_install_apps:
            d["can_install_apps"] = False
        return d


GUEST_USERNAME = "kosher-guest"


@dataclass
class GuestPolicy:
    enabled: bool = False
    uid: int | None = None
    mode: str = "whitelist"
    whitelist: list[str] = field(default_factory=list)
    rules: list[dict] = field(default_factory=list)
    blocked_categories: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        d: dict = {"enabled": self.enabled}
        if self.uid is not None:
            d["uid"] = self.uid
        if self.mode != "whitelist":
            d["mode"] = self.mode
        if self.whitelist:
            d["whitelist"] = self.whitelist
        if self.rules:
            d["rules"] = self.rules
        if self.blocked_categories:
            d["blocked_categories"] = self.blocked_categories
        return d


@dataclass
class Policy:
    revision: int = 0
    source: str = "local"
    users: list[UserPolicy] = field(default_factory=list)
    guardian_enabled: bool = False
    system_whitelist: list[str] = field(default_factory=list)
    guest: GuestPolicy = field(default_factory=GuestPolicy)

    def user(self, uid: int) -> UserPolicy | None:
        return next((u for u in self.users if u.uid == uid), None)

    def effective_users(self) -> list[UserPolicy]:
        """Managed users plus the guest account (when enabled and created) —
        what enforcement (nftables, dnsmasq, malcontent) actually applies to."""
        users = list(self.users)
        if self.guest.enabled and self.guest.uid is not None:
            users.append(UserPolicy(
                uid=self.guest.uid, username=GUEST_USERNAME,
                mode=self.guest.mode, whitelist=list(self.guest.whitelist),
                rules=list(self.guest.rules),
                blocked_categories=list(self.guest.blocked_categories),
            ))
        return users

    def effective_system_whitelist(self) -> list[str]:
        return list(dict.fromkeys([*BUILTIN_SYSTEM_WHITELIST, *self.system_whitelist]))

    def to_dict(self) -> dict:
        return {
            "schema_version": 1,
            "revision": self.revision,
            "source": self.source,
            "users": [u.to_dict() for u in self.users],
            "guardian": {"enabled": self.guardian_enabled},
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
                    apps=list(u.get("apps", [])),
                    can_install_apps=u.get("can_install_apps", True),
                    rules=list(u.get("rules", [])),
                    blocked_categories=list(u.get("blocked_categories", [])),
                    media_level=u.get("media_level", DEFAULT_MEDIA_LEVEL),
                    youtube=dict(u.get("youtube", {})),
                )
                for u in doc["users"]
            ],
            guardian_enabled=doc["guardian"]["enabled"],
            system_whitelist=list(doc.get("system_whitelist", [])),
            guest=GuestPolicy(
                enabled=guest_doc["enabled"],
                uid=guest_doc.get("uid"),
                mode=guest_doc.get("mode", "whitelist"),
                whitelist=list(guest_doc.get("whitelist", [])),
                rules=list(guest_doc.get("rules", [])),
                blocked_categories=list(guest_doc.get("blocked_categories", [])),
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
