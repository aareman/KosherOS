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
import subprocess

import gi

gi.require_version("Malcontent", "0")
from gi.repository import Gio, GLib, Malcontent  # noqa: E402

from .policy import Policy  # noqa: E402

log = logging.getLogger(__name__)


def _installed_apps() -> list[tuple[str, str]]:
    """(app id, full ref) for each installed app.

    malcontent matches blocklisted refs exactly — a wildcard like
    app/id/*/* silently matches nothing — so we need the real refs.
    """
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
    manager = Malcontent.Manager.new(Gio.bus_get_sync(Gio.BusType.SYSTEM, None))
    installed = None

    for user in policy.effective_users():
        builder = Malcontent.AppFilterBuilder.new()
        if user.admin:
            builder.set_allow_user_installation(True)
            builder.set_allow_system_installation(True)
        else:
            builder.set_allow_user_installation(False)
            builder.set_allow_system_installation(False)
            if user.apps:
                if installed is None:
                    installed = _installed_apps()
                for app_id, full_ref in installed:
                    if app_id not in user.apps:
                        builder.blocklist_flatpak_ref(full_ref)
        app_filter = builder.end()
        try:
            manager.set_app_filter(
                user.uid, app_filter, Malcontent.ManagerSetValueFlags.INTERACTIVE, None
            )
        except GLib.Error as e:
            log.error("malcontent filter for uid %d failed: %s", user.uid, e)

    log.info("malcontent filters applied for %d users", len(policy.effective_users()))
