"""In each person's session: an update is waiting for a restart (#19).

kosherd downloads updates in the background and never restarts the
computer. When one is ready, everybody signed in should be able to see it
— any account, not only administrators — without being nagged. So this
small helper runs in every graphical session as the person signed in
(kosher-update-notify.service, a systemd user unit) and does two things:

- a mark in the panel for as long as the update waits, gone after the
  restart: a StatusNotifierItem, the tray protocol GNOME's AppIndicator
  extension and Noctalia's tray widget both show. Clicking it says what it
  means and offers Restart, which asks for an administrator's password on
  anybody else's account (kosherd's Reboot is the apply-updates action).
- one pop-up when an automatic download finishes, if the administrator
  left notices on: kosherd says so with `announce`, once per version, and
  this remembers the version too, so a restarted session or helper does
  not say it again.

Everything is read from kosherd (the PendingUpdate call and the
UpdateReady signal); nothing here runs bootc.
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path

from . import updates

log = logging.getLogger("kosher-update-notify")

APP_NAME = "KosherOS"
ICON = "software-update-available-symbolic"
INDICATOR_ID = "kosheros-update"
INDICATOR_TITLE = "Update ready — restart to use it"
EXPLAIN_SUMMARY = "Update ready"
EXPLAIN_BODY = ("A new version of KosherOS is downloaded. Restart the computer "
                "to start using it. Anything open will be closed, so save your "
                "work first.")
RESTART_ACTION = "restart"
RESTART_LABEL = "Restart now"
# How often the helper asks kosherd again, in case it missed a signal
# (kosherd restarted, say). The answer is kosherd's own note: cheap.
REFRESH_SECONDS = 30 * 60


def told_path() -> Path:
    state = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    return Path(state) / "kosheros" / "update-notified"


def already_told(path: Path, digest: str) -> bool:
    try:
        return path.read_text().strip() == digest
    except OSError:
        return False


def remember(path: Path, digest: str) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(digest + "\n")
    except OSError as e:
        log.warning("could not remember the notice: %s", e)


class UpdateNotice:
    """The decisions, apart from the buses.

    `notify(summary, body, actions) -> id` sends a desktop notification,
    `indicator(on)` shows or takes away the panel mark, `reboot()` asks
    kosherd to restart.
    """

    def __init__(self, notify, indicator, reboot, told: Path):
        self._notify = notify
        self._indicator = indicator
        self._reboot = reboot
        self._told = told
        self.pending: dict | None = None
        self._ids: set[int] = set()

    def ready(self, version: str, digest: str, announce: bool) -> None:
        """kosherd's UpdateReady, or a PendingUpdate answer."""
        if not digest:
            if self.pending is not None:
                self.pending = None
                self._indicator(False)
            return
        first = self.pending is None or self.pending.get("digest") != digest
        self.pending = {"version": version, "digest": digest}
        if first:
            self._indicator(True)
        if announce and not already_told(self._told, digest):
            remember(self._told, digest)
            self._send(updates.READY_SUMMARY, updates.READY_BODY)

    def pending_answer(self, answer: dict | None) -> None:
        answer = answer or {}
        self.ready(answer.get("version") or "", answer.get("digest") or "", False)

    def activate(self) -> None:
        """The panel mark was clicked: say what it means, offer Restart."""
        if self.pending is not None:
            self._send(EXPLAIN_SUMMARY, EXPLAIN_BODY)

    def action(self, notification_id: int, key: str) -> None:
        if key == RESTART_ACTION and int(notification_id) in self._ids:
            self._reboot()

    def _send(self, summary: str, body: str) -> None:
        try:
            found = int(self._notify(summary, body, [RESTART_ACTION, RESTART_LABEL]) or 0)
        except Exception as e:  # noqa: BLE001 - a lost notice is not a reason to exit
            log.warning("could not show the notification: %s", e)
            return
        if found:
            self._ids.add(found)


SNI_XML = """
<node>
  <interface name="org.kde.StatusNotifierItem">
    <property name="Category" type="s" access="read"/>
    <property name="Id" type="s" access="read"/>
    <property name="Title" type="s" access="read"/>
    <property name="Status" type="s" access="read"/>
    <property name="WindowId" type="i" access="read"/>
    <property name="IconName" type="s" access="read"/>
    <property name="IconPixmap" type="a(iiay)" access="read"/>
    <property name="OverlayIconName" type="s" access="read"/>
    <property name="AttentionIconName" type="s" access="read"/>
    <property name="ToolTip" type="(sa(iiay)ss)" access="read"/>
    <property name="ItemIsMenu" type="b" access="read"/>
    <property name="Menu" type="o" access="read"/>
    <method name="ContextMenu">
      <arg name="x" type="i" direction="in"/>
      <arg name="y" type="i" direction="in"/>
    </method>
    <method name="Activate">
      <arg name="x" type="i" direction="in"/>
      <arg name="y" type="i" direction="in"/>
    </method>
    <method name="SecondaryActivate">
      <arg name="x" type="i" direction="in"/>
      <arg name="y" type="i" direction="in"/>
    </method>
    <method name="Scroll">
      <arg name="delta" type="i" direction="in"/>
      <arg name="orientation" type="s" direction="in"/>
    </method>
    <signal name="NewTitle"/>
    <signal name="NewIcon"/>
    <signal name="NewToolTip"/>
    <signal name="NewStatus">
      <arg name="status" type="s"/>
    </signal>
  </interface>
</node>
"""

SNI_PATH = "/StatusNotifierItem"


def sni_properties() -> dict:
    """The panel mark's StatusNotifierItem properties, as plain values
    keyed by name with their D-Bus type: (type, value)."""
    return {
        "Category": ("s", "SystemServices"),
        "Id": ("s", INDICATOR_ID),
        "Title": ("s", INDICATOR_TITLE),
        "Status": ("s", "Active"),
        "WindowId": ("i", 0),
        "IconName": ("s", ICON),
        "IconPixmap": ("a(iiay)", []),
        "OverlayIconName": ("s", ""),
        "AttentionIconName": ("s", ""),
        "ToolTip": ("(sa(iiay)ss)", (ICON, [], INDICATOR_TITLE,
                                     "Click to restart, or it is used the next "
                                     "time the computer starts.")),
        # No menu: a click is Activate, which explains and offers Restart.
        "ItemIsMenu": ("b", False),
        "Menu": ("o", "/NO_DBUSMENU"),
    }


class Indicator:  # pragma: no cover - needs a session bus and a tray host
    """A StatusNotifierItem on the session bus, shown by owning a name of
    its own and registering it with the tray's watcher, and taken away by
    giving the name up (the watcher then drops it)."""

    def __init__(self, session, on_activate):
        from gi.repository import Gio

        self._bus = session
        self._on_activate = on_activate
        self._node = Gio.DBusNodeInfo.new_for_xml(SNI_XML)
        self._name = f"org.kde.StatusNotifierItem-{os.getpid()}-1"
        self._owner = 0
        self._registration = 0
        self._watch = 0

    def set(self, on: bool) -> None:
        from gi.repository import Gio

        if on and not self._owner:
            self._registration = self._bus.register_object(
                SNI_PATH, self._node.interfaces[0], self._call, self._get, None)
            self._owner = Gio.bus_own_name_on_connection(
                self._bus, self._name, Gio.BusNameOwnerFlags.NONE,
                lambda *_a: self._register(), None)
            # A tray that starts (or restarts) later is told again.
            self._watch = Gio.bus_watch_name_on_connection(
                self._bus, "org.kde.StatusNotifierWatcher", Gio.BusNameWatcherFlags.NONE,
                lambda *_a: self._register(), None)
        elif not on and self._owner:
            Gio.bus_unown_name(self._owner)
            Gio.bus_unwatch_name(self._watch)
            self._bus.unregister_object(self._registration)
            self._owner = self._registration = self._watch = 0

    def _register(self) -> None:
        from gi.repository import Gio, GLib

        if not self._owner:
            return

        def done(bus, result):
            try:
                bus.call_finish(result)
                log.info("panel mark registered as %s", self._name)
            except GLib.Error as e:
                log.info("no tray to show the mark in yet: %s", e.message)

        # Not call_sync: the main loop must keep answering the tray, which
        # reads the mark's properties while it registers it.
        self._bus.call(
            "org.kde.StatusNotifierWatcher", "/StatusNotifierWatcher",
            "org.kde.StatusNotifierWatcher", "RegisterStatusNotifierItem",
            GLib.Variant("(s)", (self._name,)), None, Gio.DBusCallFlags.NONE, 5000,
            None, done)

    def _get(self, _conn, _sender, _path, _iface, name):
        from gi.repository import GLib

        found = sni_properties().get(name)
        return GLib.Variant(*found) if found else None

    def _call(self, _conn, _sender, _path, _iface, method, _params, invocation):
        if method in ("Activate", "SecondaryActivate", "ContextMenu"):
            self._on_activate()
        invocation.return_value(None)


def main() -> int:  # pragma: no cover - needs a session bus and a system bus
    import sys

    import gi

    gi.require_version("Gio", "2.0")
    from gi.repository import Gio, GLib

    from .client import DaemonClient

    logging.basicConfig(level=logging.INFO, format="%(name)s: %(message)s",
                        stream=sys.stderr)
    session = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    client = DaemonClient()

    def send(summary, body, actions):
        result = session.call_sync(
            "org.freedesktop.Notifications", "/org/freedesktop/Notifications",
            "org.freedesktop.Notifications", "Notify",
            GLib.Variant("(susssasa{sv}i)", (
                APP_NAME, 0, ICON, summary, body, actions,
                {"urgency": GLib.Variant("y", 1)}, -1)),
            GLib.VariantType("(u)"), Gio.DBusCallFlags.NONE, 5000, None)
        return result.unpack()[0]

    def reboot():
        def work():
            try:
                client.reboot()
            except GLib.Error as e:
                log.warning("restart refused: %s", e.message)
        threading.Thread(target=work, daemon=True).start()

    notice: UpdateNotice | None = None
    indicator = Indicator(session, lambda: notice.activate())
    notice = UpdateNotice(send, indicator.set, reboot, told_path())

    session.signal_subscribe(
        "org.freedesktop.Notifications", "org.freedesktop.Notifications", "ActionInvoked",
        "/org/freedesktop/Notifications", None, Gio.DBusSignalFlags.NONE,
        lambda _c, _s, _p, _i, _n, params: notice.action(*params.unpack()))
    client.connect_update_ready(lambda v, d, a: notice.ready(v, d, a))

    def refresh() -> bool:
        try:
            notice.pending_answer(client.pending_update())
        except GLib.Error as e:
            log.info("could not ask kosherd: %s", e.message)
        return True

    refresh()
    GLib.timeout_add_seconds(REFRESH_SECONDS, refresh)
    log.info("watching for updates waiting for a restart")
    GLib.MainLoop().run()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
