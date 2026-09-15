"""The warning on the screen before a person's time runs out.

kosherd decides when time is short and says so on the system bus
(TimeWarning: uid, minutes left, why). It cannot put anything on a
person's screen itself — notifications belong to the session — so this
small helper runs in every graphical session as the person signed in
(kosher-time-notify.service, a systemd user unit), listens for warnings
about its own account, and shows each one as an ordinary desktop
notification through org.freedesktop.Notifications.

The enforcement does not depend on this: a session that kills the helper
still gets locked and signed out on time. What the person loses by killing
it is only the warning.
"""

from __future__ import annotations

import logging
import os

log = logging.getLogger("kosher-time-notify")

APP_NAME = "KosherOS"
ICON = "alarm-symbolic"
# freedesktop urgency levels: a five-minute warning should stay on screen.
URGENCY_NORMAL = 1
URGENCY_CRITICAL = 2


def message(minutes_left: int, reason: str) -> tuple[str, str, int]:
    """(summary, body, urgency) for a warning, in the second person.

    `reason` is "limit" (today's time is used up) or "schedule" (the hours
    this account may be used are ending); anything else reads as the limit.
    """
    schedule = reason == "schedule"
    if minutes_left <= 0:
        if schedule:
            return ("Your time on the computer is over for now",
                    "The hours this account may be used have ended. Save your "
                    "work — this account will be signed out in a minute.",
                    URGENCY_CRITICAL)
        return ("Your time for today is up",
                "You have used today's time on this computer. Save your work — "
                "this account will be signed out in a minute.",
                URGENCY_CRITICAL)
    when = "1 minute" if minutes_left == 1 else f"{minutes_left} minutes"
    if schedule:
        return (f"{when} left",
                f"The hours this account may be used end in {when}. "
                "Finish up and save your work.",
                URGENCY_CRITICAL if minutes_left <= 5 else URGENCY_NORMAL)
    return (f"{when} left today",
            f"You have {when} of today's time on this computer left. "
            "Finish up and save your work.",
            URGENCY_CRITICAL if minutes_left <= 5 else URGENCY_NORMAL)


def handle(uid: int, minutes_left: int, reason: str, *, my_uid: int, notify) -> bool:
    """Show a warning if it is about this account. Returns whether it was."""
    if int(uid) != int(my_uid):
        return False
    notify(*message(int(minutes_left), str(reason)))
    return True


class Notifier:
    """Sends through org.freedesktop.Notifications, replacing the previous
    warning so the screen carries one line, not a stack of them."""

    def __init__(self, send):
        self._send = send   # (app_name, replaces_id, icon, summary, body, urgency) -> id
        self.last_id = 0

    def __call__(self, summary: str, body: str, urgency: int) -> None:
        try:
            self.last_id = int(self._send(APP_NAME, self.last_id, ICON, summary,
                                          body, urgency) or 0)
        except Exception as e:  # noqa: BLE001 - a lost warning is not a reason to exit
            log.warning("could not show the notification: %s", e)


def main() -> int:  # pragma: no cover - needs a session bus and a system bus
    import sys

    import gi

    gi.require_version("Gio", "2.0")
    from gi.repository import Gio, GLib

    from .client import DaemonClient

    logging.basicConfig(level=logging.INFO, format="%(name)s: %(message)s",
                        stream=sys.stderr)
    session = Gio.bus_get_sync(Gio.BusType.SESSION, None)

    def send(app_name, replaces_id, icon, summary, body, urgency):
        result = session.call_sync(
            "org.freedesktop.Notifications", "/org/freedesktop/Notifications",
            "org.freedesktop.Notifications", "Notify",
            GLib.Variant("(susssasa{sv}i)", (
                app_name, replaces_id, icon, summary, body, [],
                {"urgency": GLib.Variant("y", urgency)},
                0 if urgency == URGENCY_CRITICAL else 15000)),
            GLib.VariantType("(u)"), Gio.DBusCallFlags.NONE, 5000, None)
        return result.unpack()[0]

    notifier = Notifier(send)
    my_uid = os.getuid()
    DaemonClient().connect_time_signals(
        lambda uid, minutes, reason: handle(uid, minutes, reason,
                                            my_uid=my_uid, notify=notifier))
    log.info("listening for time warnings for uid %d", my_uid)
    GLib.MainLoop().run()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
