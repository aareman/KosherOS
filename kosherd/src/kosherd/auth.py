"""polkit authorization for D-Bus callers.

Every kosherd method maps to a polkit action ID declared in
/usr/share/polkit-1/actions/org.kosherlinux.policy. The shipped rule answers
yes to members of kosher-admin from their own signed-in session and asks
anyone else at the keyboard for an administrator's password (AUTH_ADMIN_KEEP);
a separate rule makes kosher-admin the administrators for these actions only,
so no stock privileged action has anyone who could authorize it.
"""

from __future__ import annotations

from gi.repository import Gio, GLib

# Action ids live in access.py, which is importable without GObject.
from .access import (  # noqa: E402,F401
    ACTION_APPLY_UPDATES,
    ACTION_INSTALL_APPS,
    ACTION_MANAGE_FILTER,
    ACTION_MANAGE_GUARDIAN,
    ACTION_MANAGE_NETWORK,
    ACTION_MANAGE_USERS,
    ACTION_READ_CONFIG,
    ACTION_USE_STORE,
)

_ALLOW_USER_INTERACTION = 1


class NotAuthorized(Exception):
    pass


def require(connection: Gio.DBusConnection, sender: str, action_id: str) -> None:
    """Raise NotAuthorized unless polkit authorizes `sender` for `action_id`.

    Interactive: on an account that is not an administrator's, polkit pops
    the desktop auth agent for an administrator's password — the call blocks
    until answered. An administrator's own session is answered at once.
    """
    # A caller already running as root has full control of the machine; a
    # polkit check adds nothing. This is also what makes kosherctl usable
    # from a root shell (support, first-boot wizard, tests) where no
    # interactive polkit agent exists.
    if caller_uid(connection, sender) == 0:
        return
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
