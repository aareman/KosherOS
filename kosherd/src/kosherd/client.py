"""D-Bus client for kosherd — shared by kosherctl and the admin app."""

from __future__ import annotations

import json

import gi

gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib

from . import BUS_NAME, OBJECT_PATH


class DaemonClient:
    def __init__(self) -> None:
        self._conn = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)

    def _call(self, iface: str, method: str, signature: str | None = None, *args):
        params = GLib.Variant(signature, args) if signature else None
        result = self._conn.call_sync(
            BUS_NAME, OBJECT_PATH, f"org.kosherlinux.Daemon1.{iface}", method,
            params, None, Gio.DBusCallFlags.NONE,
            GLib.MAXINT,  # polkit prompts block; never time out
            None,
        )
        return result.unpack() if result else ()

    # Profiles
    def get_policy(self) -> dict:
        return json.loads(self._call("Profiles", "GetPolicy")[0])

    def set_filter_mode(self, uid: int, mode: str, guardian_password: str = "") -> None:
        self._call("Profiles", "SetFilterMode", "(iss)", uid, mode, guardian_password)

    def set_whitelist(self, uid: int, domains: list[str], guardian_password: str = "") -> None:
        self._call("Profiles", "SetWhitelist", "(iass)", uid, domains, guardian_password)

    def create_child(self, username: str, full_name: str, mode: str) -> int:
        return self._call("Profiles", "CreateChild", "(sss)", username, full_name, mode)[0]

    def adopt_user(self, username: str, mode: str) -> int:
        return self._call("Profiles", "AdoptUser", "(ss)", username, mode)[0]

    def remove_user(self, uid: int) -> None:
        self._call("Profiles", "RemoveUser", "(i)", uid)

    # Apps
    def list_catalog(self) -> dict:
        return json.loads(self._call("Apps", "ListCatalog")[0])

    def install_app(self, ref: str) -> None:
        self._call("Apps", "InstallApp", "(s)", ref)

    def remove_app(self, ref: str) -> None:
        self._call("Apps", "RemoveApp", "(s)", ref)

    # System
    def check_update(self) -> str:
        return self._call("System", "CheckUpdate")[0]

    def apply_update(self) -> None:
        self._call("System", "ApplyUpdate")

    # Network
    def set_captive_mode(self, uid: int, minutes: int) -> None:
        self._call("Network", "SetCaptiveMode", "(ii)", uid, minutes)

    # Guardian
    def guardian_enabled(self) -> bool:
        return self._call("Guardian", "IsEnabled")[0]

    def set_guardian_password(self, old: str, new: str) -> None:
        self._call("Guardian", "SetGuardianPassword", "(ss)", old, new)

    def disable_guardian(self, guardian_password: str) -> None:
        self._call("Guardian", "DisableGuardian", "(s)", guardian_password)
