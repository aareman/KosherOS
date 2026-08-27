"""polkit authorization for D-Bus callers.

Every kosherd method maps to a polkit action ID declared in
/usr/share/polkit-1/actions/org.kosherlinux.policy. The shipped rule grants
members of kosher-admin AUTH_SELF_KEEP (re-enter their own password); everyone
else fails, and a separate rule ensures no polkit admin identities exist at all.
"""

from __future__ import annotations

from gi.repository import Gio, GLib

ACTION_MANAGE_USERS = "org.kosherlinux.manage-users"
ACTION_MANAGE_FILTER = "org.kosherlinux.manage-filter"
ACTION_INSTALL_APPS = "org.kosherlinux.install-apps"
ACTION_MANAGE_NETWORK = "org.kosherlinux.manage-network"
ACTION_APPLY_UPDATES = "org.kosherlinux.apply-updates"
ACTION_MANAGE_GUARDIAN = "org.kosherlinux.manage-guardian"

_ALLOW_USER_INTERACTION = 1


class NotAuthorized(Exception):
    pass


def require(connection: Gio.DBusConnection, sender: str, action_id: str) -> None:
    """Raise NotAuthorized unless polkit authorizes `sender` for `action_id`.

    Interactive: polkit will pop the desktop auth agent for the admin's own
    password (AUTH_SELF_KEEP) — the call blocks until answered.
    """
    subject = ("system-bus-name", {"name": GLib.Variant("s", sender)})
    result = connection.call_sync(
        "org.freedesktop.PolicyKit1",
        "/org/freedesktop/PolicyKit1/Authority",
        "org.freedesktop.PolicyKit1.Authority",
        "CheckAuthorization",
        GLib.Variant("((sa{sv})sa{ss}us)", (subject, action_id, {}, _ALLOW_USER_INTERACTION, "")),
        GLib.VariantType("((bba{ss}))"),
        Gio.DBusCallFlags.NONE,
        -1,  # polkit interaction can be slow; no timeout
        None,
    )
    authorized, _challenge, _details = result[0]
    if not authorized:
        raise NotAuthorized(f"not authorized for {action_id}")


def caller_uid(connection: Gio.DBusConnection, sender: str) -> int:
    result = connection.call_sync(
        "org.freedesktop.DBus",
        "/org/freedesktop/DBus",
        "org.freedesktop.DBus",
        "GetConnectionUnixUser",
        GLib.Variant("(s)", (sender,)),
        GLib.VariantType("(u)"),
        Gio.DBusCallFlags.NONE,
        -1,
        None,
    )
    return result[0]
