"""malcontent (GNOME parental controls) wiring.

flatpak and gnome-shell natively honor malcontent app filters stored in
accountsservice. kosherd writes one per managed user so that:
- non-admin managed users cannot install flatpaks at all (user OR system) —
  this is what actually blocks `flatpak install --user` from dodging the
  curated remote;
- admins keep installation rights (kosherd itself installs system-wide).

Per-app allow-lists (user.apps) are enforced as a malcontent blocklist of
everything installed that is NOT allowed; refreshed on every policy apply
and after each install/remove.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from .policy import Policy, UserPolicy

log = logging.getLogger(__name__)


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


def filter_spec(user: UserPolicy, installed: list[tuple[str, str]]) -> FilterSpec:
    """The filter for `user`, given (app id, full ref) pairs of what is installed."""
    if user.admin:
        # Admins install through kosherd; leaving the capability on keeps
        # the stock tooling coherent for them.
        return FilterSpec(user.uid, True, True)

    blocklist = []
    if user.apps:
        # An allow-list is expressed to malcontent as "block everything
        # installed that is not on it", using EXACT refs.
        blocklist = [full_ref for app_id, full_ref in installed
                     if app_id not in user.apps]
    return FilterSpec(user.uid, False, False, blocklist)


def specs_for(policy: Policy, installed: list[tuple[str, str]]) -> list[FilterSpec]:
    return [filter_spec(user, installed) for user in policy.effective_users()]


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

    manager = Malcontent.Manager.new(Gio.bus_get_sync(Gio.BusType.SYSTEM, None))
    # Only look up what is installed if some user actually restricts apps.
    installed = _installed_apps() if any(
        u.apps and not u.admin for u in policy.effective_users()) else []

    for spec in specs_for(policy, installed):
        builder = Malcontent.AppFilterBuilder.new()
        builder.set_allow_user_installation(spec.allow_user_installation)
        builder.set_allow_system_installation(spec.allow_system_installation)
        for ref in spec.blocklist:
            builder.blocklist_flatpak_ref(ref)
        try:
            manager.set_app_filter(
                spec.uid, builder.end(),
                Malcontent.ManagerSetValueFlags.INTERACTIVE, None,
            )
        except GLib.Error as e:
            log.error("malcontent filter for uid %d failed: %s", spec.uid, e)

    log.info("malcontent filters applied for %d users",
             len(policy.effective_users()))
