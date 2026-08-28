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

    # Session — one polkit prompt, then a sliding-timeout admin session
    def unlock(self) -> None:
        self._call("Session", "Unlock")

    def lock(self) -> None:
        self._call("Session", "Lock")

    def session_status(self) -> tuple[bool, int, bool]:
        return self._call("Session", "Status")

    def verify_guardian(self, password: str) -> None:
        self._call("Session", "VerifyGuardian", "(s)", password)

    # Profiles
    def get_policy(self) -> dict:
        return json.loads(self._call("Profiles", "GetPolicy")[0])

    def set_filter_mode(self, uid: int, mode: str, guardian_password: str = "") -> None:
        self._call("Profiles", "SetFilterMode", "(iss)", uid, mode, guardian_password)

    def set_whitelist(self, uid: int, domains: list[str], guardian_password: str = "") -> None:
        self._call("Profiles", "SetWhitelist", "(iass)", uid, domains, guardian_password)

    def create_user(self, username: str, full_name: str, mode: str) -> int:
        return self._call("Profiles", "CreateUser", "(sss)", username, full_name, mode)[0]

    def set_guest_config(self, enabled: bool, mode: str, whitelist: list[str],
                         guardian_password: str = "") -> None:
        self._call("Profiles", "SetGuestConfig", "(bsass)",
                   enabled, mode, whitelist, guardian_password)

    def adopt_user(self, username: str, mode: str) -> int:
        return self._call("Profiles", "AdoptUser", "(ss)", username, mode)[0]

    def remove_user(self, uid: int) -> None:
        self._call("Profiles", "RemoveUser", "(i)", uid)

    # Apps
    def list_catalog(self) -> dict:
        return json.loads(self._call("Apps", "ListCatalog")[0])

    def list_installed(self) -> list[str]:
        return self._call("Apps", "ListInstalled")[0]

    def install_app(self, ref: str) -> None:
        """Start an install; watch AppProgress/AppFinished for the outcome."""
        self._call("Apps", "InstallApp", "(s)", ref)

    def remove_app(self, ref: str) -> None:
        self._call("Apps", "RemoveApp", "(s)", ref)

    def set_user_can_install(self, uid: int, can_install: bool) -> None:
        self._call("Apps", "SetUserCanInstall", "(ib)", uid, can_install)

    def connect_app_signals(self, on_progress, on_finished) -> int:
        """Subscribe to install progress. Returns a subscription id."""

        def handler(_conn, _sender, _path, _iface, signal, params):
            if signal == "AppProgress":
                on_progress(*params.unpack())
            elif signal == "AppFinished":
                on_finished(*params.unpack())

        return self._conn.signal_subscribe(
            BUS_NAME, "org.kosherlinux.Daemon1.Apps", None, OBJECT_PATH, None,
            Gio.DBusSignalFlags.NONE, handler,
        )

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
