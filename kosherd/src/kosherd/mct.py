"""malcontent (GNOME parental controls) wiring.

flatpak and gnome-shell natively honor malcontent app filters stored in
accountsservice. kosherd writes one per managed user so that:
- non-admin managed users cannot install flatpaks at all (user OR system) —
  this is what actually blocks `flatpak install --user` from dodging
  kosherd;
- admins keep installation rights (kosherd itself installs system-wide).

What an account may run is the same rule as what it may install
(appaccess.decide): the approved list or the whole store, minus blocked
kinds and blocked apps. It is enforced as a malcontent blocklist of every
installed app the rule refuses, refreshed on every policy apply and after
each install/remove. The older per-user allow-list (user.apps) is still
honoured for a policy that has not been converted yet.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Iterable

from . import appaccess
from .policy import Policy, UserPolicy

log = logging.getLogger(__name__)

# Programs only an administrator should see in the app grid.
ADMIN_ONLY_PROGRAMS = ("/usr/bin/kosher-admin",)


@dataclass
class FilterSpec:
    """What one user's malcontent filter should say.

    Deciding this is pure, so it is separated from talking to malcontent:
    the per-user blocklist once used a wildcard ref (app/id/*/*), which
    malcontent silently never matches, and "blocked" apps stayed runnable.
    """

    uid: int
    allow_user_installation: bool
    allow_system_installation: bool
    blocklist: list[str] = field(default_factory=list)
    blocked_paths: list[str] = field(default_factory=list)


def _entry(app_id: str, index: dict[str, dict] | None, approved: dict[str, dict]) -> dict:
    """What is known about an installed app: its index entry, else its
    approved-list entry, else a bare ref (which decide() fails closed on)."""
    if index and app_id in index:
        return index[app_id]
    if app_id in approved:
        return approved[app_id]
    return {"ref": app_id}


def filter_spec(user: UserPolicy, installed: list[tuple[str, str]],
                index: dict[str, dict] | None = None,
                approved: Iterable[dict] = ()) -> FilterSpec:
    """The filter for `user`, given (app id, full ref) pairs of what is
    installed, the app index by ref (None when the machine has not loaded
    one yet) and the approved list's entries."""
    if user.admin:
        # Admins install through kosherd; leaving the capability on keeps
        # the stock tooling coherent for them, and restricting their own
        # launcher would only lock them out of fixing it.
        return FilterSpec(user.uid, True, True)

    approved_by_ref = {a["ref"]: a for a in approved if a.get("ref")}
    blocklist = []
    for app_id, full_ref in installed:
        if user.apps and app_id not in user.apps:
            # The older allow-list: "block everything installed that is
            # not on it", using EXACT refs.
            blocklist.append(full_ref)
            continue
        entry = _entry(app_id, index, approved_by_ref)
        if appaccess.decide(user, entry, approved_by_ref) is not None:
            blocklist.append(full_ref)
    # Hide the admin app from people who cannot use it: left in the app grid
    # it invites someone to open it and meet a password prompt they can never
    # satisfy, which reads as broken rather than "not for you".
    return FilterSpec(user.uid, False, False, blocklist,
                      list(ADMIN_ONLY_PROGRAMS))


def specs_for(policy: Policy, installed: list[tuple[str, str]],
              index: dict[str, dict] | None = None,
              approved: Iterable[dict] = ()) -> list[FilterSpec]:
    approved = list(approved)
    return [filter_spec(user, installed, index, approved)
            for user in policy.effective_users()]


def _installed_apps() -> list[tuple[str, str]]:
    """(app id, full ref) for each installed app."""
    import gi as _gi

    _gi.require_version("Flatpak", "1.0")
    from gi.repository import Flatpak

    installation = Flatpak.Installation.new_system(None)
    return [
        (r.get_name(), r.format_ref())
        for r in installation.list_installed_refs(None)
        if r.get_kind() == Flatpak.RefKind.APP
    ]


def apply_malcontent(policy: Policy) -> None:
    """Write app filters for all managed users. Raises on manager failure."""
    import gi

    gi.require_version("Malcontent", "0")
    from gi.repository import Gio, GLib, Malcontent

    from . import apps

    manager = Malcontent.Manager.new(Gio.bus_get_sync(Gio.BusType.SYSTEM, None))
    # Only look up what is installed if some user is restricted at all.
    restricted = any(not u.admin for u in policy.effective_users())
    installed = _installed_apps() if restricted else []
    # The index already in memory, never a parse here: this runs on the
    # policy path, and a forty-megabyte parse does not belong on it. The
    # daemon re-applies once the index is warm.
    index = apps.cached_index_by_ref() if restricted else None
    approved = apps.load_catalog().get("apps", []) if restricted else []

    for spec in specs_for(policy, installed, index, approved):
        builder = Malcontent.AppFilterBuilder.new()
        builder.set_allow_user_installation(spec.allow_user_installation)
        builder.set_allow_system_installation(spec.allow_system_installation)
        for ref in spec.blocklist:
            builder.blocklist_flatpak_ref(ref)
        for path in spec.blocked_paths:
            # malcontent renamed these (blacklist -> blocklist); accept
            # either so the image is not pinned to one library version.
            for name in ("blocklist_path", "blacklist_path"):
                method = getattr(builder, name, None)
                if method is not None:
                    method(path)
                    break
            else:
                log.warning("malcontent cannot block paths; %s stays visible",
                            path)
        try:
            manager.set_app_filter(
                spec.uid, builder.end(),
                Malcontent.ManagerSetValueFlags.INTERACTIVE, None,
            )
        except GLib.Error as e:
            log.error("malcontent filter for uid %d failed: %s", spec.uid, e)

    log.info("malcontent filters applied for %d users",
             len(policy.effective_users()))
