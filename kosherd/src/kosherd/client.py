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

    # First-boot setup (only available until an admin exists)
    def setup_complete(self) -> bool:
        return self._call("Setup", "IsComplete")[0]

    def admin_exists(self) -> tuple[bool, str]:
        """(exists, username) — lets an interrupted wizard skip the account step."""
        return tuple(self._call("Setup", "AdminExists"))

    def existing_accounts(self) -> list[str]:
        """Login accounts an installer or image created before setup."""
        return list(self._call("Setup", "ExistingAccounts")[0])

    def create_first_admin(self, username: str, full_name: str, password: str) -> int:
        return self._call("Setup", "CreateFirstAdmin", "(sss)",
                          username, full_name, password)[0]

    def network_status(self) -> dict:
        """Is this machine online, and by what; whether it has Wi-Fi at all."""
        return json.loads(self._call("Setup", "NetworkStatus")[0])

    def list_wifi(self) -> list[dict]:
        """Networks in range: ssid, signal, secured, active. Rescans."""
        return json.loads(self._call("Setup", "ListWifi")[0])

    def connect_wifi(self, ssid: str, password: str = "") -> None:
        self._call("Setup", "ConnectWifi", "(ss)", ssid, password)

    def finish_setup(self, guardian_password: str, grub_password: str) -> None:
        self._call("Setup", "FinishSetup", "(ss)", guardian_password, grub_password)

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

    def list_whitelist_bundles(self) -> list[dict]:
        return json.loads(self._call("Profiles", "ListWhitelistBundles")[0])

    def set_whitelist_bundles(self, uid: int, bundles: list[str],
                              guardian_password: str = "") -> None:
        self._call("Profiles", "SetWhitelistBundles", "(iass)", uid, bundles,
                   guardian_password)

    def set_url_rules(self, uid: int, rules: list[dict], guardian_password: str = "") -> None:
        self._call("Profiles", "SetUrlRules", "(iss)",
                   uid, json.dumps(rules), guardian_password)

    def set_blocked_categories(self, uid: int, categories: list[str],
                               guardian_password: str = "") -> None:
        self._call("Profiles", "SetBlockedCategories", "(iass)",
                   uid, categories, guardian_password)

    def get_list_edits(self, list_name: str) -> dict:
        return json.loads(self._call("Profiles", "GetListEdits", "(s)",
                                     list_name)[0])

    def edit_list(self, list_name: str, add, remove: list[str],
                  guardian_password: str = "") -> None:
        self._call("Profiles", "EditList", "(ssass)", list_name,
                   json.dumps(add), remove, guardian_password)

    def filter_status(self) -> dict:
        return json.loads(self._call("Profiles", "FilterStatus")[0])

    def get_my_settings(self) -> dict:
        """The caller's own filter settings. No privilege, no password."""
        return json.loads(self._call("Profiles", "GetMySettings")[0])

    def list_requests(self) -> list[dict]:
        return json.loads(self._call("Profiles", "ListRequests")[0])

    def approve_request(self, request_id: str, whole_site: bool = False,
                        guardian_password: str = "") -> None:
        self._call("Profiles", "ApproveRequest", "(sbs)", request_id,
                   whole_site, guardian_password)

    def dismiss_request(self, request_id: str) -> None:
        self._call("Profiles", "DismissRequest", "(s)", request_id)

    def allow_url(self, uid: int, url: str, whole_site: bool = False,
                  guardian_password: str = "") -> None:
        self._call("Profiles", "AllowUrl", "(isbs)", uid, url, whole_site,
                   guardian_password)

    def list_activity(self, since: int = 0, uid: int = -1) -> list[dict]:
        return json.loads(self._call("Profiles", "ListActivity", "(ii)",
                                     since, uid)[0])

    def activity_summary(self) -> dict:
        """Today's counts, keyed by uid as a string (JSON keys)."""
        return json.loads(self._call("Profiles", "ActivitySummary")[0])

    def set_media_level(self, uid: int, level: str,
                        guardian_password: str = "") -> None:
        self._call("Profiles", "SetMediaLevel", "(iss)", uid, level,
                   guardian_password)

    def set_language_filter(self, uid: int, setting: str,
                            guardian_password: str = "") -> None:
        self._call("Profiles", "SetLanguageFilter", "(iss)", uid, setting,
                   guardian_password)

    def set_youtube(self, uid: int, settings: dict,
                    guardian_password: str = "") -> None:
        self._call("Profiles", "SetYouTube", "(iss)", uid,
                   json.dumps(settings), guardian_password)

    def set_user_admin(self, uid: int, admin: bool,
                       guardian_password: str = "") -> None:
        self._call("Profiles", "SetUserAdmin", "(ibs)", uid, admin,
                   guardian_password)

    def set_layout(self, uid: int, layout: str) -> None:
        self._call("Profiles", "SetLayout", "(is)", uid, layout)

    def set_cover_style(self, uid: int, style: str) -> None:
        self._call("Profiles", "SetCoverStyle", "(is)", uid, style)

    def set_adblock(self, enabled: bool, guardian_password: str = "") -> None:
        """Ads and trackers blocked at the resolvers, for every account."""
        self._call("Profiles", "SetAdBlock", "(bs)", enabled, guardian_password)

    def set_time_limits(self, uid: int, settings: dict,
                        guardian_password: str = "") -> None:
        """{"daily_minutes": n, "allowed": [7 day strings]} — see timelimits.py."""
        self._call("Profiles", "SetTimeLimits", "(iss)", uid,
                   json.dumps(settings), guardian_password)

    def time_usage(self) -> dict:
        """Today's time per account, keyed by uid as a string (JSON keys)."""
        return json.loads(self._call("Profiles", "GetTimeUsage")[0])

    def connect_time_signals(self, on_warning) -> int:
        """Subscribe to TimeWarning(uid, minutes_left, reason)."""

        def handler(_conn, _sender, _path, _iface, signal, params):
            if signal == "TimeWarning":
                on_warning(*params.unpack())

        return self._conn.signal_subscribe(
            BUS_NAME, "org.kosherlinux.Daemon1.Profiles", "TimeWarning", OBJECT_PATH,
            None, Gio.DBusSignalFlags.NONE, handler,
        )

    def my_layout(self) -> str:
        """The calling user's own desktop layout (any active local user)."""
        return self._call("Profiles", "GetMyLayout")[0]

    def apply_profile(self, uid: int, profile: str,
                      guardian_password: str = "") -> None:
        self._call("Profiles", "ApplyProfile", "(iss)", uid, profile,
                   guardian_password)

    def list_profiles(self) -> list[dict]:
        return json.loads(self._call("Profiles", "ListProfiles")[0])

    def save_profile(self, uid: int, label: str, description: str = "",
                     guardian_password: str = "") -> str:
        """Snapshot this account's settings as a named preset; returns its key."""
        return self._call("Profiles", "SaveProfile", "(isss)", uid, label,
                          description, guardian_password)[0]

    def delete_profile(self, key: str, guardian_password: str = "") -> None:
        self._call("Profiles", "DeleteProfile", "(ss)", key, guardian_password)

    def list_categories(self) -> dict:
        return json.loads(self._call("Profiles", "ListCategories")[0])

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

    def list_installed_details(self) -> list[dict]:
        return json.loads(self._call("Apps", "ListInstalledDetails")[0])

    def set_user_apps(self, uid: int, refs: list[str]) -> None:
        self._call("Apps", "SetUserApps", "(ias)", uid, refs)

    def install_app(self, ref: str) -> None:
        """Start an install; watch AppProgress/AppFinished for the outcome."""
        self._call("Apps", "InstallApp", "(s)", ref)

    def remove_app(self, ref: str) -> None:
        self._call("Apps", "RemoveApp", "(s)", ref)

    def set_user_can_install(self, uid: int, can_install: bool) -> None:
        self._call("Apps", "SetUserCanInstall", "(ib)", uid, can_install)

    def search_apps(self, query: str) -> list[dict]:
        return json.loads(self._call("Apps", "SearchApps", "(s)", query)[0])

    def approve_app(self, ref: str, name: str = "", summary: str = "") -> None:
        self._call("Apps", "ApproveApp", "(sss)", ref, name, summary)

    def unapprove_app(self, ref: str) -> None:
        self._call("Apps", "UnapproveApp", "(s)", ref)

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
        """Start the update. It runs on in the daemon; progress arrives on
        the signals connect_update_signals subscribes to."""
        self._call("System", "ApplyUpdate")

    def disconnect_signals(self, subscription: int) -> None:
        """Drop a subscription from connect_app_signals or
        connect_update_signals, for a page that is going away."""
        self._conn.signal_unsubscribe(subscription)

    def connect_update_signals(self, on_progress, on_finished) -> int:
        """Subscribe to update progress: on_progress(percent, status) as it
        runs (percent -1 when bootc cannot say), on_finished(ok, error)
        once. Returns a subscription id."""

        def handler(_conn, _sender, _path, _iface, signal, params):
            if signal == "UpdateProgress":
                on_progress(*params.unpack())
            elif signal == "UpdateFinished":
                on_finished(*params.unpack())

        return self._conn.signal_subscribe(
            BUS_NAME, "org.kosherlinux.Daemon1.System", None, OBJECT_PATH, None,
            Gio.DBusSignalFlags.NONE, handler,
        )

    def deployment_status(self) -> dict:
        """Which image is booted, and what a rollback would return to."""
        return json.loads(self._call("System", "DeploymentStatus")[0])

    def rollback(self) -> None:
        self._call("System", "Rollback")

    def reboot(self) -> None:
        """Restart the computer (after an update is staged)."""
        self._call("System", "Reboot")

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

    # Portal
    def enrol(self, portal_url: str, code: str, guardian_password: str = "") -> str:
        return self._call("Portal", "Enrol", "(sss)",
                          portal_url, code, guardian_password)[0]

    def unenrol(self, guardian_password: str = "") -> None:
        self._call("Portal", "Unenrol", "(s)", guardian_password)

    def sync_now(self) -> bool:
        return self._call("Portal", "SyncNow")[0]

    def portal_status(self) -> dict:
        return json.loads(self._call("Portal", "PortalStatus")[0])
