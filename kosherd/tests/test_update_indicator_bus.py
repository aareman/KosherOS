"""The panel mark on a real (private) session bus (#19).

A throwaway bus from Gio.TestDBus, never the desktop's own: a stand-in
tray watcher records what registers, and the mark is read and clicked the
way GNOME's AppIndicator extension and Noctalia's tray do it.
"""

from __future__ import annotations

import shutil
import time

import pytest

gi = pytest.importorskip("gi")
gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib  # noqa: E402

from kosherd import updatenotify  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("dbus-daemon") is None,
                                reason="needs dbus-daemon for a private bus")

WATCHER_XML = """
<node><interface name="org.kde.StatusNotifierWatcher">
  <method name="RegisterStatusNotifierItem"><arg type="s" direction="in"/></method>
</interface></node>"""


def _spin(until, seconds=5.0):
    ctx = GLib.MainContext.default()
    end = time.monotonic() + seconds
    while not until() and time.monotonic() < end:
        ctx.iteration(False)
        time.sleep(0.005)
    return until()


def _call(conn, name, path, iface, method, args, reply_type=None):
    """A call that lets the main loop run meanwhile: the mark answers on
    this same thread, so a call_sync would wait on itself."""
    box = []
    conn.call(name, path, iface, method, args,
              GLib.VariantType(reply_type) if reply_type else None,
              Gio.DBusCallFlags.NONE, 2000, None,
              lambda c, res: box.append(c.call_finish(res)))
    assert _spin(lambda: box), f"{method} got no answer"
    return box[0]


@pytest.fixture
def bus():
    test_bus = Gio.TestDBus.new(Gio.TestDBusFlags.NONE)
    test_bus.up()
    try:
        conn = Gio.DBusConnection.new_for_address_sync(
            test_bus.get_bus_address(),
            Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT
            | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION, None, None)
        yield conn
        conn.close_sync(None)
    finally:
        test_bus.down()


def test_the_mark_registers_answers_and_goes_away(bus):
    registered = []
    node = Gio.DBusNodeInfo.new_for_xml(WATCHER_XML)

    def watcher_call(_c, sender, _p, _i, method, params, invocation):
        registered.append((sender, params.unpack()[0]))
        invocation.return_value(None)

    bus.register_object("/StatusNotifierWatcher", node.interfaces[0], watcher_call, None, None)
    owned = []
    Gio.bus_own_name_on_connection(bus, "org.kde.StatusNotifierWatcher",
                                   Gio.BusNameOwnerFlags.NONE,
                                   lambda *_a: owned.append(True), None)
    assert _spin(lambda: owned)

    clicks = []
    mark = updatenotify.Indicator(bus, lambda: clicks.append(True))
    mark.set(True)
    assert _spin(lambda: registered), "the mark never registered with the tray"
    name = registered[0][1]
    assert name.startswith("org.kde.StatusNotifierItem-")

    # Read as a tray reads it: by the name it registered.
    title = _call(bus, name, "/StatusNotifierItem", "org.freedesktop.DBus.Properties",
                  "Get", GLib.Variant("(ss)", ("org.kde.StatusNotifierItem", "Title")), "(v)")
    assert title.unpack()[0] == "Update ready — restart to use it"
    _call(bus, name, "/StatusNotifierItem", "org.kde.StatusNotifierItem", "Activate",
          GLib.Variant("(ii)", (0, 0)))
    assert clicks == [True]

    def has_owner():
        reply = bus.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus",
                              "org.freedesktop.DBus", "NameHasOwner",
                              GLib.Variant("(s)", (name,)), GLib.VariantType("(b)"),
                              Gio.DBusCallFlags.NONE, 2000, None)
        return reply.unpack()[0]

    assert has_owner()
    mark.set(False)
    assert _spin(lambda: not has_owner()), "the mark stayed after the update went"
