"""kosherd — the privileged management daemon.

Runs as root, owns org.kosherlinux.Daemon1 on the system bus. Every method is
gated by polkit (see auth.py); filter-weakening methods additionally require
the guardian password when guardian mode is enabled. The daemon is the ONLY
writer of enforcement state — admin app, kosherctl, and the future portal sync
agent are all just D-Bus clients of it.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
from pathlib import Path

import gi

gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib

from . import BUS_NAME, OBJECT_PATH, auth, policy as policy_mod
from .apply import apply_policy
from .guardian import Guardian, GuardianError
from .policy import MODES, Policy, PolicyError, UserPolicy

log = logging.getLogger("kosherd")

CATALOG_PATH = Path("/etc/kosher/catalog.json")
FLATPAK_REMOTE = "kosher"

INTROSPECTION_XML = """
<node>
  <interface name="org.kosherlinux.Daemon1.Profiles">
    <method name="GetPolicy">
      <arg direction="out" type="s" name="policy_json"/>
    </method>
    <method name="SetFilterMode">
      <arg direction="in" type="i" name="uid"/>
      <arg direction="in" type="s" name="mode"/>
      <arg direction="in" type="s" name="guardian_password"/>
    </method>
    <method name="SetWhitelist">
      <arg direction="in" type="i" name="uid"/>
      <arg direction="in" type="as" name="domains"/>
      <arg direction="in" type="s" name="guardian_password"/>
    </method>
    <method name="CreateChild">
      <arg direction="in" type="s" name="username"/>
      <arg direction="in" type="s" name="full_name"/>
      <arg direction="in" type="s" name="mode"/>
      <arg direction="out" type="i" name="uid"/>
    </method>
    <method name="RemoveUser">
      <arg direction="in" type="i" name="uid"/>
    </method>
    <signal name="PolicyChanged">
      <arg type="i" name="revision"/>
    </signal>
  </interface>
  <interface name="org.kosherlinux.Daemon1.Apps">
    <method name="ListCatalog">
      <arg direction="out" type="s" name="catalog_json"/>
    </method>
    <method name="InstallApp">
      <arg direction="in" type="s" name="ref"/>
    </method>
    <method name="RemoveApp">
      <arg direction="in" type="s" name="ref"/>
    </method>
  </interface>
  <interface name="org.kosherlinux.Daemon1.System">
    <method name="CheckUpdate">
      <arg direction="out" type="s" name="status"/>
    </method>
    <method name="ApplyUpdate"/>
  </interface>
  <interface name="org.kosherlinux.Daemon1.Network">
    <method name="SetCaptiveMode">
      <arg direction="in" type="i" name="uid"/>
      <arg direction="in" type="i" name="minutes"/>
    </method>
  </interface>
  <interface name="org.kosherlinux.Daemon1.Guardian">
    <method name="IsEnabled">
      <arg direction="out" type="b" name="enabled"/>
    </method>
    <method name="SetGuardianPassword">
      <arg direction="in" type="s" name="old_password"/>
      <arg direction="in" type="s" name="new_password"/>
    </method>
    <method name="DisableGuardian">
      <arg direction="in" type="s" name="guardian_password"/>
    </method>
  </interface>
</node>
"""

# method name -> polkit action id
ACTIONS = {
    "GetPolicy": auth.ACTION_MANAGE_USERS,
    "SetFilterMode": auth.ACTION_MANAGE_FILTER,
    "SetWhitelist": auth.ACTION_MANAGE_FILTER,
    "CreateChild": auth.ACTION_MANAGE_USERS,
    "RemoveUser": auth.ACTION_MANAGE_USERS,
    "ListCatalog": auth.ACTION_INSTALL_APPS,
    "InstallApp": auth.ACTION_INSTALL_APPS,
    "RemoveApp": auth.ACTION_INSTALL_APPS,
    "CheckUpdate": auth.ACTION_APPLY_UPDATES,
    "ApplyUpdate": auth.ACTION_APPLY_UPDATES,
    "SetCaptiveMode": auth.ACTION_MANAGE_NETWORK,
    "IsEnabled": auth.ACTION_MANAGE_GUARDIAN,
    "SetGuardianPassword": auth.ACTION_MANAGE_GUARDIAN,
    "DisableGuardian": auth.ACTION_MANAGE_GUARDIAN,
}

# Methods that can weaken the filter: guardian password required when enabled.
GUARDIAN_GATED = {"SetFilterMode", "SetWhitelist", "DisableGuardian"}

ERROR_NAME = "org.kosherlinux.Daemon1.Error"


class Daemon:
    def __init__(self) -> None:
        self.policy = policy_mod.load()
        self.guardian = Guardian()
        self.connection: Gio.DBusConnection | None = None

    # ---- lifecycle -------------------------------------------------------

    def run(self) -> int:
        loop = GLib.MainLoop()
        node = Gio.DBusNodeInfo.new_for_xml(INTROSPECTION_XML)
        owner_id = Gio.bus_own_name(
            Gio.BusType.SYSTEM,
            BUS_NAME,
            Gio.BusNameOwnerFlags.NONE,
            lambda conn, name: self._on_bus_acquired(conn, node),
            None,
            lambda conn, name: (log.error("lost bus name %s", name), loop.quit()),
        )
        try:
            apply_policy(self.policy)
        except Exception:
            log.exception("failed to apply policy at startup; baseline rules remain")
        try:
            loop.run()
        finally:
            Gio.bus_unown_name(owner_id)
        return 0

    def _on_bus_acquired(self, connection: Gio.DBusConnection, node: Gio.DBusNodeInfo) -> None:
        self.connection = connection
        for iface in node.interfaces:
            connection.register_object(OBJECT_PATH, iface, self._handle_call, None, None)
        log.info("kosherd listening on %s", BUS_NAME)

    # ---- dispatch --------------------------------------------------------

    def _handle_call(self, connection, sender, path, iface, method, params, invocation) -> None:
        try:
            auth.require(connection, sender, ACTIONS[method])
            args = list(params.unpack())
            if method in GUARDIAN_GATED and self.policy.guardian_enabled:
                # guardian_password is always the trailing string argument
                if not self.guardian.verify(args[-1]):
                    raise GuardianError("guardian password incorrect")
            result = getattr(self, f"impl_{method}")(*args)
            invocation.return_value(result)
            log.info("%s by %s (uid %s): ok", method, sender, auth.caller_uid(connection, sender))
        except (auth.NotAuthorized, GuardianError, PolicyError, KeyError, ValueError) as e:
            log.warning("%s by %s refused: %s", method, sender, e)
            invocation.return_dbus_error(ERROR_NAME, str(e))
        except Exception as e:  # noqa: BLE001 - daemon must not crash on a bad call
            log.exception("%s failed", method)
            invocation.return_dbus_error(ERROR_NAME, f"internal error: {e}")

    def _save_and_apply(self) -> None:
        policy_mod.save(self.policy)
        apply_policy(self.policy)
        if self.connection:
            self.connection.emit_signal(
                None, OBJECT_PATH, "org.kosherlinux.Daemon1.Profiles", "PolicyChanged",
                GLib.Variant("(i)", (self.policy.revision,)),
            )

    # ---- Profiles --------------------------------------------------------

    def impl_GetPolicy(self):
        return GLib.Variant("(s)", (json.dumps(self.policy.to_dict()),))

    def impl_SetFilterMode(self, uid: int, mode: str, _guardian_pw: str):
        if mode not in MODES:
            raise PolicyError(f"unknown mode {mode!r}")
        user = self.policy.user(uid)
        if user is None:
            raise PolicyError(f"uid {uid} is not managed")
        user.mode = mode
        self._save_and_apply()
        return None

    def impl_SetWhitelist(self, uid: int, domains: list[str], _guardian_pw: str):
        user = self.policy.user(uid)
        if user is None:
            raise PolicyError(f"uid {uid} is not managed")
        user.whitelist = sorted(set(domains))
        self._save_and_apply()
        return None

    def impl_CreateChild(self, username: str, full_name: str, mode: str):
        if mode not in MODES:
            raise PolicyError(f"unknown mode {mode!r}")
        uid = self._accounts_create_user(username, full_name)
        self.policy.users.append(UserPolicy(uid=uid, username=username, mode=mode))
        self._save_and_apply()
        return GLib.Variant("(i)", (uid,))

    def impl_RemoveUser(self, uid: int):
        user = self.policy.user(uid)
        if user is None:
            raise PolicyError(f"uid {uid} is not managed")
        self._accounts_delete_user(uid)
        self.policy.users.remove(user)
        self._save_and_apply()
        return None

    def _accounts_create_user(self, username: str, full_name: str) -> int:
        # accountsservice: CreateUser(name, fullname, accountType=0 standard)
        path = self.connection.call_sync(
            "org.freedesktop.Accounts", "/org/freedesktop/Accounts",
            "org.freedesktop.Accounts", "CreateUser",
            GLib.Variant("(ssi)", (username, full_name, 0)),
            GLib.VariantType("(o)"), Gio.DBusCallFlags.NONE, -1, None,
        )[0]
        uid = self.connection.call_sync(
            "org.freedesktop.Accounts", path,
            "org.freedesktop.DBus.Properties", "Get",
            GLib.Variant("(ss)", ("org.freedesktop.Accounts.User", "Uid")),
            GLib.VariantType("(v)"), Gio.DBusCallFlags.NONE, -1, None,
        )[0].get_uint64()
        return int(uid)

    def _accounts_delete_user(self, uid: int) -> None:
        self.connection.call_sync(
            "org.freedesktop.Accounts", "/org/freedesktop/Accounts",
            "org.freedesktop.Accounts", "DeleteUser",
            GLib.Variant("(xb)", (uid, True)),
            None, Gio.DBusCallFlags.NONE, -1, None,
        )

    # ---- Apps ------------------------------------------------------------

    def _catalog(self) -> dict:
        try:
            return json.loads(CATALOG_PATH.read_text())
        except FileNotFoundError:
            return {"apps": []}

    def impl_ListCatalog(self):
        return GLib.Variant("(s)", (json.dumps(self._catalog()),))

    def impl_InstallApp(self, ref: str):
        allowed = {a["ref"] for a in self._catalog()["apps"]}
        if ref not in allowed:
            raise PolicyError(f"{ref} is not in the approved catalog")
        self._flatpak("install", FLATPAK_REMOTE, ref)
        return None

    def impl_RemoveApp(self, ref: str):
        self._flatpak("uninstall", ref)
        return None

    @staticmethod
    def _flatpak(*args: str) -> None:
        res = subprocess.run(
            ["flatpak", args[0], "--system", "--noninteractive", "-y", *args[1:]],
            capture_output=True, text=True,
        )
        if res.returncode != 0:
            raise PolicyError(f"flatpak {args[0]} failed: {res.stderr.strip()}")

    # ---- System ----------------------------------------------------------

    def impl_CheckUpdate(self):
        res = subprocess.run(["bootc", "upgrade", "--check"], capture_output=True, text=True)
        return GLib.Variant("(s)", (res.stdout or res.stderr,))

    def impl_ApplyUpdate(self):
        res = subprocess.run(["bootc", "upgrade"], capture_output=True, text=True)
        if res.returncode != 0:
            raise PolicyError(f"bootc upgrade failed: {res.stderr.strip()}")
        return None

    # ---- Network ---------------------------------------------------------

    def impl_SetCaptiveMode(self, uid: int, minutes: int):
        if not 1 <= minutes <= 60:
            raise PolicyError("captive window must be 1-60 minutes")
        res = subprocess.run(
            ["nft", "add", "element", "inet", "kosher", "captive",
             f"{{ {uid} timeout {minutes}m }}"],
            capture_output=True, text=True,
        )
        if res.returncode != 0:
            raise PolicyError(f"could not open captive window: {res.stderr.strip()}")
        return None

    # ---- Guardian --------------------------------------------------------

    def impl_IsEnabled(self):
        return GLib.Variant("(b)", (self.policy.guardian_enabled,))

    def impl_SetGuardianPassword(self, old_password: str, new_password: str):
        self.guardian.set_password(new_password, old_password or None)
        if not self.policy.guardian_enabled:
            self.policy.guardian_enabled = True
            self._save_and_apply()
        return None

    def impl_DisableGuardian(self, _guardian_pw: str):
        self.policy.guardian_enabled = False
        self._save_and_apply()
        return None


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(name)s: %(message)s", stream=sys.stderr)
    return Daemon().run()


if __name__ == "__main__":
    sys.exit(main())
