"""Which apps an account may install and run.

Two settings on an account decide what the Store shows it and what
kosherd will install for it:

- `app_access`: "approved" — only the apps an administrator has approved
  (the strict default for a new account: it is today's allowlist, and a
  family that never opens the Apps tab gets exactly that); or "store" —
  everything on Flathub, minus what is blocked.
- what is blocked: whole kinds of app (`blocked_app_kinds`, the Store's
  shelves: Games, Internet, ...) and single apps (`blocked_apps`). Both
  apply in either access mode and to running as well as installing, so an
  approved chess game is still gone for an account that blocks games.

Opening the whole store has to be safe on its own, without a parent
going through three thousand apps. Flathub rates every app with OARS
(the Open Age Ratings Service: nudity, sexual themes, violence, drugs,
language, gambling, each as none/mild/moderate/intense), so kosherd holds
a fixed ceiling per attribute and refuses anything above it. A short list
of tools whose purpose is to route around a filter is refused the same
way. An explicit approval by an administrator overrides both — that is
what approving an app means — so a family that wants one particular game
above the ceiling approves it and moves on.

`decide` is the one place the rule lives; the Store, the installer and
malcontent all ask it.
"""

from __future__ import annotations

from typing import Iterable

from . import appkinds

APP_ACCESS = ("approved", "store")
DEFAULT_APP_ACCESS = "approved"
# What a new administrator's own account gets; admins are unrestricted at
# run time regardless (see mct.py), and can put themselves on the approved
# list like anyone else.
ADMIN_APP_ACCESS = "store"
APP_ACCESS_LABELS = {
    "approved": "Approved apps only",
    "store": "The whole store, minus what is blocked",
}

# OARS intensities, in order.
INTENSITY = {"none": 0, "mild": 1, "moderate": 2, "intense": 3}

# The highest OARS intensity an unapproved app may declare for each
# attribute. Attributes not listed are not judged: money-purchasing,
# money-advertising and the social-* attributes describe what an app can
# do (buy things, show ads, chat) rather than what it shows, and a parent
# controls those through the Internet kind and by blocking single apps.
# Every messenger declares social-chat, so a ceiling there would empty a
# shelf the family expects to find.
CEILING = {
    "sex-nudity": "none",
    "sex-themes": "none",
    "sex-homosexuality": "none",
    "sex-prostitution": "none",
    "sex-adultery": "none",
    "sex-appearance": "none",
    "violence-sexual": "none",
    "violence-bloodshed": "none",
    "violence-desecration": "none",
    "violence-slavery": "mild",
    "violence-worship": "mild",
    "violence-realistic": "mild",
    "violence-fantasy": "moderate",
    "violence-cartoon": "moderate",
    "drugs-narcotics": "none",
    "drugs-tobacco": "mild",
    "drugs-alcohol": "mild",
    "language-profanity": "none",
    "language-humor": "mild",
    "language-discrimination": "none",
    "money-gambling": "none",
}

# Plain words for a refused attribute, for the Store's and the admin app's
# explanations.
ATTRIBUTE_WORDS = {
    "sex-nudity": "nudity",
    "sex-themes": "sexual themes",
    "sex-homosexuality": "sexual themes",
    "sex-prostitution": "sexual themes",
    "sex-adultery": "sexual themes",
    "sex-appearance": "sexualised appearance",
    "violence-sexual": "sexual violence",
    "violence-bloodshed": "graphic violence",
    "violence-desecration": "graphic violence",
    "violence-slavery": "slavery",
    "violence-worship": "violence",
    "violence-realistic": "realistic violence",
    "violence-fantasy": "violence",
    "violence-cartoon": "violence",
    "drugs-narcotics": "drugs",
    "drugs-tobacco": "tobacco",
    "drugs-alcohol": "alcohol",
    "language-profanity": "bad language",
    "language-humor": "crude humour",
    "language-discrimination": "discriminatory language",
    "money-gambling": "gambling",
}

# Apps whose purpose is to get around a network filter. The web filter
# already blocks proxy and VPN *sites* for every filtered account (the
# "proxy" category in categories.py); these are the same thing as apps.
# Refused unless an administrator approves one by name.
CIRCUMVENTION = frozenset({
    "org.torproject.torbrowser-launcher",
    "com.github.micahflee.torbrowser-launcher",
    "org.onionshare.OnionShare",
    "net.mullvad.MullvadVPN",
    "com.protonvpn.www",
    "com.windscribe.Windscribe",
})

# Why an app is not available to an account. Keys the Store and the admin
# app turn into a sentence; the values are those sentences.
REASONS = {
    "blocked": "blocked for this account",
    "kind": "its kind of app is blocked for this account",
    "not-approved": "not on the approved list",
    "content": "rated above what this computer allows",
    "circumvention": "a tool for getting around the filter",
    "unknown": "not in the app list this computer has downloaded",
}


def _get(account, name: str, default=None):
    """A setting from a UserPolicy or from the dict form of one (what the
    admin app and the portal hold)."""
    if isinstance(account, dict):
        value = account.get(name, default)
        return default if value is None else value
    return getattr(account, name, default)


def access_of(account) -> str:
    """The account's app access, with the default filled in: the approved
    list for everyone, the whole store for an administrator who has not
    chosen otherwise."""
    chosen = _get(account, "app_access")
    if chosen in APP_ACCESS:
        return chosen
    return ADMIN_APP_ACCESS if _get(account, "admin", False) else DEFAULT_APP_ACCESS


def content_reasons(app: dict) -> list[str]:
    """The OARS attributes of `app` above the ceiling, worst first."""
    rating = app.get("rating") or {}
    found = []
    for attribute, value in rating.items():
        limit = CEILING.get(attribute)
        if limit is None:
            continue
        if INTENSITY.get(value, 0) > INTENSITY[limit]:
            found.append((INTENSITY.get(value, 0), attribute))
    found.sort(reverse=True)
    return [attribute for _intensity, attribute in found]


def content_words(app: dict) -> str:
    """'nudity and bad language' — what a rating refusal is about."""
    words = list(dict.fromkeys(ATTRIBUTE_WORDS.get(a, a) for a in content_reasons(app)))
    if not words:
        return ""
    if len(words) == 1:
        return words[0]
    return ", ".join(words[:-1]) + " and " + words[-1]


def decide(account, app: dict, approved: Iterable[str]) -> str | None:
    """Why `account` may not have `app`, or None when it may.

    `app` is the app's index entry ({ref, categories, rating, ...}), or a
    bare {"ref": ...} when the app list does not know it; `approved` is
    the approved list. Blocks the administrator set come first and apply
    whatever the access mode; an approval then settles it; otherwise the
    approved-only account is refused and the store account is judged by
    the rating ceiling.
    """
    ref = app.get("ref") or ""
    if ref in set(_get(account, "blocked_apps") or ()):
        return "blocked"
    kinds = set(_get(account, "blocked_app_kinds") or ())
    if kinds and appkinds.kind_of(app) in kinds:
        return "kind"
    if ref in set(approved):
        return None
    if access_of(account) != "store":
        return "not-approved"
    if ref in CIRCUMVENTION:
        return "circumvention"
    if not (app.get("known") or "categories" in app or "rating" in app):
        # Nothing to judge it by: an index entry always carries its
        # categories, so a bare ref means the app list does not know the
        # app. Fail closed rather than let it through because the machine
        # is offline.
        return "unknown"
    if content_reasons(app):
        return "content"
    return None


def explain(reason: str, app: dict) -> str:
    """One sentence for a refusal, with the rating spelled out."""
    name = app.get("name") or app.get("ref") or "This app"
    if reason == "content":
        return f"{name} is rated for {content_words(app)}, which this computer does not allow"
    return f"{name} is {REASONS.get(reason, reason)}"


def visible(account, index: Iterable[dict], approved_entries: Iterable[dict]) -> list[dict]:
    """The apps `account` may install, with a `kind` on each.

    The approved list is part of the answer in both modes — it is the
    whole answer for an approved-only account, and for a store account it
    also covers an app the downloaded index does not carry (a machine that
    has been offline still shows its approved apps).
    """
    approved = {a["ref"]: a for a in approved_entries}
    indexed = {app["ref"]: app for app in index}
    by_ref: dict[str, dict] = dict(indexed) if access_of(account) == "store" else {}
    for ref, entry in approved.items():
        known = indexed.get(ref)
        # The index knows the name, categories, icon and rating as the
        # publisher declared them; the approved list fills in for an app
        # the index does not carry.
        merged = {**entry, **(known or {})}
        merged.setdefault("categories", [])
        by_ref[ref] = merged
    out = []
    for ref, app in by_ref.items():
        if decide(account, app, approved) is None:
            out.append({**app, "kind": appkinds.kind_of(app),
                        "approved": ref in approved})
    out.sort(key=lambda a: (a.get("name") or a["ref"]).lower())
    return out
