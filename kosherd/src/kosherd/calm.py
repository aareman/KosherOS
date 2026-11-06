"""Calmer versions of the addictive sites (#179).

Sites like x.com, reddit, YouTube and facebook are built to be bottomless:
the feed never ends, the sidebar recommends, the comments pull you down.
They cannot always be blocked wholesale — an article links a tweet you
need to see, a reddit post holds the answer to a bug — so the account
gets a calmer version of the site instead:

- A feed page ends. After a set number of items the page is over, and the
  ads, the sidebar, the trending and the recommendations never show.
- A direct link to a post renders as the post alone: no comments, no
  navigation, no links back into the site. The tweet an article linked is
  there to read; the rabbit hole is not.
- A site may carry a daily time budget on top, counted while a page of it
  is actually on screen. When the budget is spent the site is done for
  the day.
- Login flows are never touched: none of this applies on them.

The pages are single-page apps — what they draw never passes the proxy
as HTML — so the rewriting happens in the page, by a script the proxy
injects (mitm/kosher_calm.js). This module is the part both sides agree
on: which hosts are which site, which paths are a post and which a feed,
and what a policy's `calm` settings may look like.
"""

from __future__ import annotations

import re

# The sites, in the order the admin app lists them. `hosts` are root
# domains: a host matches when it is one of them or ends below one, so
# www., m., old. and mobile. all come along. `login` prefixes are never
# touched at all. `single` patterns name a path that shows ONE post —
# those get the clean render; anything else that is not a login page is
# treated as a feed and ends after the item cap.
SITES: dict[str, dict] = {
    "x": {
        "label": "X (Twitter)",
        "hosts": ("x.com", "twitter.com"),
        "login": ("/i/flow/", "/login", "/logout", "/signup", "/i/oauth",
                  "/account/begin_password_reset"),
        "single": (re.compile(r"^/[^/]+/status/\d+"),
                   re.compile(r"^/i/web/status/\d+")),
    },
    "reddit": {
        "label": "Reddit",
        "hosts": ("reddit.com",),
        "login": ("/login", "/register", "/auth", "/account/"),
        "single": (re.compile(r"^/r/[^/]+/comments/"),
                   re.compile(r"^/comments/"),
                   re.compile(r"^/user/[^/]+/comments/")),
    },
    "youtube": {
        "label": "YouTube",
        "hosts": ("youtube.com", "youtube-nocookie.com"),
        # Signing in happens on accounts.google.com, off the site.
        "login": (),
        "single": (re.compile(r"^/watch"), re.compile(r"^/shorts/"),
                   re.compile(r"^/embed/"), re.compile(r"^/live/")),
    },
    "facebook": {
        "label": "Facebook",
        "hosts": ("facebook.com",),
        "login": ("/login",),
        "single": (re.compile(r"^/[^/]+/(posts|videos|photos)/"),
                   re.compile(r"^/permalink"), re.compile(r"^/photo"),
                   re.compile(r"^/story"), re.compile(r"^/reel/")),
    },
}

SITE_ORDER = ("x", "reddit", "youtube", "facebook")

# A feed ends after this many items unless the policy says otherwise.
DEFAULT_FEED_ITEMS = 30
MIN_FEED_ITEMS = 5
MAX_FEED_ITEMS = 500
# Daily budgets, in minutes; 0 means no limit.
MAX_DAILY_MINUTES = 1440


def site_for(host: str | None) -> str | None:
    """Which site `host` belongs to, or None. Suffix match on the root
    domains, so www., m. and old. all count; lookalikes do not."""
    host = (host or "").lower().rstrip(".")
    if not host:
        return None
    for key, spec in SITES.items():
        for root in spec["hosts"]:
            if host == root or host.endswith("." + root):
                return key
    return None


def is_login(site: str, path: str) -> bool:
    """A sign-in or account flow, which none of this may ever touch."""
    path = (path or "").split("?", 1)[0].lower()
    return any(path.startswith(prefix) for prefix in SITES[site]["login"])


def is_single(site: str, path: str) -> bool:
    """A page that shows one post: it gets the clean render, not a cap."""
    path = (path or "").split("?", 1)[0]
    return any(pattern.search(path) for pattern in SITES[site]["single"])


def mode_for(site: str, path: str) -> str:
    """How a page of the site is treated: "login" (left alone), "single"
    (a post on its own, rendered clean) or "feed" (a listing, capped)."""
    if is_login(site, path):
        return "login"
    return "single" if is_single(site, path) else "feed"


def normalize(settings) -> dict:
    """Validate and canonicalise a policy's `calm` settings.

    The shape: {"enabled": bool, "feed_items": int, "sites": {<site>:
    {"daily_minutes": int}}}. A site not listed is left as it is. Raises
    ValueError on anything the admin app could not have sent.
    """
    if settings in (None, {}):
        return {}
    if not isinstance(settings, dict):
        raise ValueError("calm settings must be an object")
    unknown = set(settings) - {"enabled", "feed_items", "sites"}
    if unknown:
        raise ValueError(f"unknown calm settings: {sorted(unknown)}")
    out: dict = {}
    if settings.get("enabled"):
        out["enabled"] = True
    feed_items = settings.get("feed_items", DEFAULT_FEED_ITEMS)
    if isinstance(feed_items, bool) or not isinstance(feed_items, int) \
            or not MIN_FEED_ITEMS <= feed_items <= MAX_FEED_ITEMS:
        raise ValueError(
            f"feed_items must be {MIN_FEED_ITEMS}..{MAX_FEED_ITEMS}")
    if feed_items != DEFAULT_FEED_ITEMS:
        out["feed_items"] = feed_items
    sites = settings.get("sites") or {}
    if not isinstance(sites, dict):
        raise ValueError("calm sites must be an object")
    unknown = set(sites) - set(SITES)
    if unknown:
        raise ValueError(f"unknown calm sites: {sorted(unknown)}")
    kept: dict[str, dict] = {}
    for key in SITE_ORDER:
        entry = sites.get(key)
        if entry is None:
            continue
        if entry is True:  # tolerate {"x": true}
            entry = {}
        if not isinstance(entry, dict):
            raise ValueError(f"calm site {key!r} must be an object")
        unknown = set(entry) - {"daily_minutes"}
        if unknown:
            raise ValueError(f"unknown calm settings for {key}: {sorted(unknown)}")
        minutes = entry.get("daily_minutes", 0)
        if isinstance(minutes, bool) or not isinstance(minutes, int) \
                or not 0 <= minutes <= MAX_DAILY_MINUTES:
            raise ValueError(
                f"daily_minutes for {key} must be 0..{MAX_DAILY_MINUTES}")
        kept[key] = {"daily_minutes": minutes} if minutes else {}
    if kept:
        out["sites"] = kept
    if not settings.get("enabled"):
        # The master switch is off: the rest is kept only as a draft by the
        # admin app, and a draft does not travel in the policy.
        return {}
    if not kept:
        # On for nothing is off: the checklist in the admin app is the
        # sites, so an empty one means the feature does nothing.
        out.pop("enabled")
    return out


def enabled_sites(settings: dict | None) -> list[str]:
    """The sites the calmer version applies to, in display order."""
    if not settings or not settings.get("enabled"):
        return []
    return [key for key in SITE_ORDER if key in (settings.get("sites") or {})]


def applies(settings: dict | None, site: str) -> bool:
    return site in enabled_sites(settings)


def feed_items(settings: dict | None) -> int:
    value = (settings or {}).get("feed_items", DEFAULT_FEED_ITEMS)
    return value if isinstance(value, int) and not isinstance(value, bool) \
        else DEFAULT_FEED_ITEMS


def daily_minutes(settings: dict | None, site: str) -> int:
    """The site's daily budget in minutes; 0 when there is no limit."""
    entry = ((settings or {}).get("sites") or {}).get(site) or {}
    value = entry.get("daily_minutes", 0)
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def label(site: str) -> str:
    return SITES[site]["label"]


def path_rules(site: str) -> dict:
    """The site's path rules as the injected script needs them (#179).

    These sites are single-page apps: clicking a post does not load a
    page, so the script has to work out the new mode itself. The rules
    come from here, not from a second copy in the JavaScript, so the two
    cannot drift.
    """
    spec = SITES[site]
    return {"login": list(spec["login"]),
            "single": [pattern.pattern for pattern in spec["single"]]}
