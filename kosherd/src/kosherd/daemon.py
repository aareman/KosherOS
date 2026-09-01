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

from . import BUS_NAME, OBJECT_PATH, access, apps, auth, policy as policy_mod
from .apply import apply_policy
from .apps import AppError
from .guardian import Guardian, GuardianError
from .policy import INSPECTED_MODES, MODES, Policy, PolicyError, UserPolicy
from .session import SessionStore

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
    <method name="SetBlockedCategories">
      <arg direction="in" type="i" name="uid"/>
      <arg direction="in" type="as" name="categories"/>
      <arg direction="in" type="s" name="guardian_password"/>
    </method>
    <method name="GetListEdits">
      <arg direction="in" type="s" name="list_name"/>
      <arg direction="out" type="s" name="edits_json"/>
    </method>
    <method name="EditList">
      <arg direction="in" type="s" name="list_name"/>
      <arg direction="in" type="s" name="add_json"/>
      <arg direction="in" type="as" name="remove"/>
      <arg direction="in" type="s" name="guardian_password"/>
    </method>
    <method name="FilterStatus">
      <arg direction="out" type="s" name="status_json"/>
    </method>
    <method name="ListRequests">
      <arg direction="out" type="s" name="requests_json"/>
    </method>
    <method name="ApproveRequest">
      <arg direction="in" type="s" name="request_id"/>
      <arg direction="in" type="b" name="whole_site"/>
      <arg direction="in" type="s" name="guardian_password"/>
    </method>
    <method name="DismissRequest">
      <arg direction="in" type="s" name="request_id"/>
    </method>
    <method name="SetMediaLevel">
      <arg direction="in" type="i" name="uid"/>
      <arg direction="in" type="s" name="level"/>
      <arg direction="in" type="s" name="guardian_password"/>
    </method>
    <method name="SetLanguageFilter">
      <arg direction="in" type="i" name="uid"/>
      <arg direction="in" type="s" name="setting"/>
      <arg direction="in" type="s" name="guardian_password"/>
    </method>
    <method name="SetYouTube">
      <arg direction="in" type="i" name="uid"/>
      <arg direction="in" type="s" name="settings_json"/>
      <arg direction="in" type="s" name="guardian_password"/>
    </method>
    <method name="SetUserAdmin">
      <arg direction="in" type="i" name="uid"/>
      <arg direction="in" type="b" name="admin"/>
      <arg direction="in" type="s" name="guardian_password"/>
    </method>
    <method name="ApplyProfile">
      <arg direction="in" type="i" name="uid"/>
      <arg direction="in" type="s" name="profile"/>
      <arg direction="in" type="s" name="guardian_password"/>
    </method>
    <method name="ListProfiles">
      <arg direction="out" type="s" name="profiles_json"/>
    </method>
    <method name="ListCategories">
      <arg direction="out" type="s" name="categories_json"/>
    </method>
    <method name="SetUrlRules">
      <arg direction="in" type="i" name="uid"/>
      <arg direction="in" type="s" name="rules_json"/>
      <arg direction="in" type="s" name="guardian_password"/>
    </method>
    <method name="CreateUser">
      <arg direction="in" type="s" name="username"/>
      <arg direction="in" type="s" name="full_name"/>
      <arg direction="in" type="s" name="mode"/>
      <arg direction="out" type="i" name="uid"/>
    </method>
    <method name="SetGuestConfig">
      <arg direction="in" type="b" name="enabled"/>
      <arg direction="in" type="s" name="mode"/>
      <arg direction="in" type="as" name="whitelist"/>
      <arg direction="in" type="s" name="guardian_password"/>
    </method>
    <method name="AdoptUser">
      <arg direction="in" type="s" name="username"/>
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
    <method name="ListInstalled">
      <arg direction="out" type="as" name="refs"/>
    </method>
    <method name="ListInstalledDetails">
      <arg direction="out" type="s" name="details_json"/>
    </method>
    <method name="SetUserApps">
      <arg direction="in" type="i" name="uid"/>
      <arg direction="in" type="as" name="refs"/>
    </method>
    <method name="InstallApp">
      <arg direction="in" type="s" name="ref"/>
    </method>
    <method name="RemoveApp">
      <arg direction="in" type="s" name="ref"/>
    </method>
    <method name="SetUserCanInstall">
      <arg direction="in" type="i" name="uid"/>
      <arg direction="in" type="b" name="can_install"/>
    </method>
    <method name="SearchApps">
      <arg direction="in" type="s" name="query"/>
      <arg direction="out" type="s" name="results_json"/>
    </method>
    <method name="ApproveApp">
      <arg direction="in" type="s" name="ref"/>
      <arg direction="in" type="s" name="name"/>
      <arg direction="in" type="s" name="summary"/>
    </method>
    <method name="UnapproveApp">
      <arg direction="in" type="s" name="ref"/>
    </method>
    <signal name="AppProgress">
      <arg type="s" name="ref"/>
      <arg type="i" name="percent"/>
      <arg type="s" name="status"/>
    </signal>
    <signal name="AppFinished">
      <arg type="s" name="ref"/>
      <arg type="b" name="ok"/>
      <arg type="s" name="error"/>
    </signal>
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
  <interface name="org.kosherlinux.Daemon1.Portal">
    <method name="Enrol">
      <arg direction="in" type="s" name="portal_url"/>
      <arg direction="in" type="s" name="code"/>
      <arg direction="in" type="s" name="guardian_password"/>
      <arg direction="out" type="s" name="device_id"/>
    </method>
    <method name="Unenrol">
      <arg direction="in" type="s" name="guardian_password"/>
    </method>
    <method name="SyncNow">
      <arg direction="out" type="b" name="updated"/>
    </method>
    <method name="PortalStatus">
      <arg direction="out" type="s" name="status_json"/>
    </method>
  </interface>
  <interface name="org.kosherlinux.Daemon1.Setup">
    <method name="IsComplete">
      <arg direction="out" type="b" name="complete"/>
    </method>
    <method name="CreateFirstAdmin">
      <arg direction="in" type="s" name="username"/>
      <arg direction="in" type="s" name="full_name"/>
      <arg direction="in" type="s" name="password"/>
      <arg direction="out" type="i" name="uid"/>
    </method>
    <method name="FinishSetup">
      <arg direction="in" type="s" name="guardian_password"/>
      <arg direction="in" type="s" name="grub_password"/>
    </method>
  </interface>
  <interface name="org.kosherlinux.Daemon1.Session">
    <method name="Unlock"/>
    <method name="Lock"/>
    <method name="Status">
      <arg direction="out" type="b" name="unlocked"/>
      <arg direction="out" type="i" name="seconds_remaining"/>
      <arg direction="out" type="b" name="guardian_satisfied"/>
    </method>
    <method name="VerifyGuardian">
      <arg direction="in" type="s" name="password"/>
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
# The gating tables and decision live in access.py so they can be tested
# without a D-Bus bus; see tests/test_access.py.
ACTIONS = access.ACTIONS
UID_AWARE = access.UID_AWARE

SETUP_STAMP = Path("/var/lib/kosher/setup-complete")
GRUB_USER_CFG = Path("/boot/grub2/user.cfg")

ERROR_NAME = "org.kosherlinux.Daemon1.Error"



def _edit_size(add) -> int:
    """How many entries a family's additions hold, in any list's shape.

    Flat for the word list ({word: replacement}) and the search terms
    ([term, ...]); nested for the content terms ({level: {weight: [...]}}).
    """
    if isinstance(add, list):
        return len(add)
    if not isinstance(add, dict):
        return 0
    total = 0
    for value in add.values():
        if isinstance(value, dict):        # a level, holding weights
            total += sum(len(words) if isinstance(words, list) else 1
                         for words in value.values())
        elif isinstance(value, list):
            total += len(value)
        else:
            total += 1
    return total


def _list_size(doc) -> int:
    """How many entries a list document holds, whatever its shape."""
    if isinstance(doc, list):
        return len(doc)
    if not isinstance(doc, dict):
        return 0
    if isinstance(doc.get("replacements"), dict):
        return len(doc["replacements"])
    terms = doc.get("terms")
    if isinstance(terms, list):
        return len(terms)
    if isinstance(terms, dict):
        return sum(len(words) for by_weight in terms.values()
                   for words in by_weight.values())
    return sum(len(v) if isinstance(v, (list, dict)) else 1
               for v in doc.values())


def _url_pattern(url: str) -> str:
    """An allow rule for one page rather than a whole site."""
    from urllib.parse import urlsplit

    parts = urlsplit(url)
    host = (parts.hostname or "").lower().strip(".").removeprefix("www.")
    path = parts.path or "/"
    return f"{host}{path.rstrip('/') or '/'}"


def _default_categories(mode: str) -> tuple[str, ...]:
    """What a new account of this mode blocks before anyone configures it.

    An admin who creates an account and walks away should still get a
    filter; "filtered" that filters nothing is the worst outcome, because
    it looks protected and is not.
    """
    from .categories import DEFAULT_BLOCKED

    return DEFAULT_BLOCKED if mode in ("dnsfilter", "filtered", "whitelist") else ()


class Daemon:
    def __init__(self) -> None:
        self.policy = policy_mod.load()
        self.guardian = Guardian()
        self.sessions = SessionStore()
        self.app_manager = apps.AppManager(self._on_app_progress, self._on_app_finished)
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
        self._apply_mct()
        try:
            apps.write_remote_filter()
        except Exception:
            log.exception("could not apply the flatpak remote filter")
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
            uid = auth.caller_uid(connection, sender)
            requirement = access.evaluate(
                method,
                setup_complete=self.setup_complete(),
                session_unlocked=self.sessions.is_unlocked(uid),
                guardian_enabled=self.policy.guardian_enabled,
                guardian_proven=self.sessions.has_guardian(uid),
            )
            if not requirement.allowed:
                raise PolicyError(requirement.refusal)
            if requirement.polkit_action is not None:
                auth.require(connection, sender, requirement.polkit_action)
            args = list(params.unpack())
            if requirement.needs_guardian:
                # guardian_password is the trailing string argument.
                if not self.guardian.verify(args[-1]):
                    raise GuardianError("guardian password incorrect")
                self.sessions.grant_guardian(uid)
            result = getattr(self, f"impl_{method}")(*args, _uid=uid) \
                if method in UID_AWARE else getattr(self, f"impl_{method}")(*args)
            invocation.return_value(result)
            log.info("%s by %s (uid %s): ok", method, sender, uid)
        except (auth.NotAuthorized, GuardianError, PolicyError, AppError, KeyError, ValueError) as e:
            log.warning("%s by %s refused: %s", method, sender, e)
            invocation.return_dbus_error(ERROR_NAME, str(e))
        except GLib.Error as e:
            # A system service we called (accountsservice, polkit, ...) said no —
            # pass its message through instead of masking it as 'internal error'.
            log.warning("%s by %s failed downstream: %s", method, sender, e.message)
            invocation.return_dbus_error(ERROR_NAME, e.message)
        except Exception as e:  # noqa: BLE001 - daemon must not crash on a bad call
            log.exception("%s failed", method)
            invocation.return_dbus_error(ERROR_NAME, f"internal error: {e}")

    def _save_and_apply(self) -> None:
        policy_mod.save(self.policy)
        apply_policy(self.policy)
        self._apply_mct()
        if self.connection:
            self.connection.emit_signal(
                None, OBJECT_PATH, "org.kosherlinux.Daemon1.Profiles", "PolicyChanged",
                GLib.Variant("(i)", (self.policy.revision,)),
            )

    def _apply_mct(self) -> None:
        try:
            from .mct import apply_malcontent

            apply_malcontent(self.policy)
        except Exception:  # noqa: BLE001 - app filters must not block the firewall path
            log.exception("malcontent application failed; network policy is still applied")

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

    # Which lists a family may edit, and how many entries an edit may hold.
    # "They can add or remove, but a handful tops" is the design: a cap
    # this low is not a limitation, it is the statement that a family who
    # needs to add fifty words has been given a list that is not finished.
    EDITABLE_LISTS = ("wordlist.json", "search-blocklist.json",
                      "content-terms.json")
    MAX_EDITS = 100

    def impl_GetListEdits(self, list_name: str):
        from . import lists

        if list_name not in self.EDITABLE_LISTS:
            raise PolicyError(f"{list_name} cannot be edited here")
        shipped, override = lists.read(list_name)
        edits = override if lists.is_delta(override) else {}
        return GLib.Variant("(s)", (json.dumps({
            "add": edits.get("add") or {},
            "remove": edits.get("remove") or [],
            "shipped": _list_size(shipped),
        }),))

    def impl_EditList(self, list_name: str, add_json: str, remove: list,
                      _guardian_pw: str):
        """Record a family's additions and removals for one list.

        Stored as a delta on top of whatever ships, so a family that adds
        one word keeps receiving every later improvement to the list. A
        copy of the whole list would freeze them at today's version and
        nobody would notice for a year.
        """
        from . import lists

        if list_name not in self.EDITABLE_LISTS:
            raise PolicyError(f"{list_name} cannot be edited here")
        try:
            add = json.loads(add_json or "{}")
        except ValueError as e:
            raise PolicyError(f"not valid JSON: {e}") from None
        if not isinstance(add, (dict, list)):
            raise PolicyError("additions must be an object or a list")
        if _edit_size(add) > self.MAX_EDITS or len(remove) > self.MAX_EDITS:
            raise PolicyError(
                f"at most {self.MAX_EDITS} entries may be added or removed; "
                "a list that needs more than that is not finished, and "
                "should be fixed for everybody rather than here")
        lists.save_delta(list_name, add=add, remove=list(remove))
        # The proxy and the search service reload on their own, but the
        # resolver's category blocks are rendered from the policy.
        self._save_and_apply()
        log.info("edited %s: +%s -%s", list_name, len(add), len(remove))
        return None

    def impl_FilterStatus(self):
        """What is actually being enforced right now, as opposed to configured.

        A filter that has quietly stopped doing something is worse than one
        that never did it, because the family is relying on it. Everything
        here is a fact about this machine at this moment, not a setting.
        """
        from . import vision

        status = {"pictures": vision.NO_MODEL, "detect_ms": None,
                  "services": {}}
        try:
            status.update(json.loads(vision.STATUS_PATH.read_text()))
        except (OSError, ValueError):
            # Nothing has been judged yet, or nobody is in a mode that
            # judges. Neither is a fault; say so rather than guess.
            status["pictures"] = "unknown"

        from . import mitmca, selfcheck

        from . import lists as lists_mod

        found = selfcheck.datasets()
        status["lists"] = found
        status["lists_version"] = lists_mod.portal_version()
        from . import catalogsync

        status["catalog_version"] = catalogsync.installed_version()
        status["problems"] = selfcheck.problems(found)

        for service in ("kosher-mitm.service", "kosher-dns.service",
                        "kosher-search.service", "kosher-searxng.service"):
            result = subprocess.run(["systemctl", "is-active", service],
                                    capture_output=True, text=True)
            status["services"][service] = result.stdout.strip() or "unknown"

        needed = {
            "kosher-mitm.service": any(u.mode in INSPECTED_MODES
                                       for u in self.policy.effective_users()),
            "kosher-dns.service": True,
            "kosher-search.service": any(
                u.mode not in ("unfiltered", "none")
                for u in self.policy.effective_users()),
        }
        # Only when somebody is actually inspected: a certificate nobody
        # needs is not a fault, and a warning that does not matter teaches
        # people to ignore warnings.
        if any(u.mode in INSPECTED_MODES for u in self.policy.effective_users()):
            ok, why = mitmca.installed()
            status["inspection_ca"] = "ok" if ok else "broken"
            if why:
                status["problems"].append(why)
        else:
            status["inspection_ca"] = "not needed"

        status["degraded"] = [
            name for name, must_run in needed.items()
            if must_run and status["services"].get(name) != "active"
        ]
        return GLib.Variant("(s)", (json.dumps(status),))

    def impl_ListRequests(self):
        from . import accessreq

        found = []
        for document in accessreq.pending():
            user = self.policy.user(document["uid"])
            found.append({**document,
                          "username": user.username if user else "?",
                          "mode": user.mode if user else "?"})
        return GLib.Variant("(s)", (json.dumps(found),))

    def impl_ApproveRequest(self, request_id: str, whole_site: bool,
                            _guardian_pw: str):
        """Grant a request, in the terms of the account's own mode.

        A whitelist account needs the domain on its whitelist; a filtered
        account needs an allow rule ahead of whatever blocked it. Making
        the admin work out which is exactly the friction that gets a filter
        switched off.
        """
        from urllib.parse import urlsplit

        from . import accessreq

        document = accessreq.resolve(request_id)
        if document is None:
            raise PolicyError("that request is no longer waiting")
        user = self.policy.user(document["uid"])
        if user is None:
            raise PolicyError("that account is no longer managed")

        parts = urlsplit(document["url"])
        host = (parts.hostname or "").lower().strip(".")
        if not host:
            raise PolicyError("that request has no address")

        if user.mode == "whitelist":
            if host not in user.whitelist:
                user.whitelist = sorted({*user.whitelist, host})
        else:
            pattern = host if whole_site else _url_pattern(document["url"])
            if not any(r.get("pattern") == pattern and r.get("action") == "allow"
                       for r in user.rules):
                # Ahead of the existing rules: a rule added after the one
                # that blocked the page would never be reached.
                user.rules = [{"action": "allow", "pattern": pattern},
                              *user.rules]
        self._save_and_apply()
        log.info("approved access for uid %d to %s", user.uid, host)
        return None

    def impl_DismissRequest(self, request_id: str):
        from . import accessreq

        accessreq.resolve(request_id)
        return None

    def _managed(self, uid: int):
        user = self.policy.user(uid)
        if user is None:
            raise PolicyError(f"uid {uid} is not managed")
        return user

    def impl_SetMediaLevel(self, uid: int, level: str, _guardian_pw: str):
        from .policy import MEDIA_LEVELS

        if level not in MEDIA_LEVELS:
            raise PolicyError(f"unknown media level {level!r}")
        self._managed(uid).media_level = level
        self._save_and_apply()
        return None

    def impl_SetLanguageFilter(self, uid: int, setting: str, _guardian_pw: str):
        from .language import MODES as LANGUAGE_MODES

        if setting not in LANGUAGE_MODES:
            raise PolicyError(f"unknown language setting {setting!r}")
        self._managed(uid).language_filter = setting
        self._save_and_apply()
        return None

    def impl_SetYouTube(self, uid: int, settings_json: str, _guardian_pw: str):
        from .policy import YOUTUBE_CATEGORIES

        try:
            settings = json.loads(settings_json or "{}")
        except ValueError as e:
            raise PolicyError(f"not valid JSON: {e}") from None
        if not isinstance(settings, dict):
            raise PolicyError("YouTube settings must be an object")
        restrict = settings.get("restrict", "moderate")
        if restrict not in ("none", "moderate", "strict"):
            raise PolicyError(f"unknown restricted mode {restrict!r}")
        unknown = set(settings.get("blocked_categories", [])) - set(YOUTUBE_CATEGORIES)
        if unknown:
            raise PolicyError(f"unknown YouTube categories: {sorted(unknown)}")
        self._managed(uid).youtube = settings
        self._save_and_apply()
        return None

    def impl_ListProfiles(self):
        from . import profiles

        described = profiles.describe()
        for entry in described:
            entry["default"] = entry["key"] == profiles.DEFAULT_PROFILE
        return GLib.Variant("(s)", (json.dumps(described),))

    def impl_ApplyProfile(self, uid: int, profile_key: str, _guardian_pw: str):
        from . import profiles

        user = self.policy.user(uid)
        if user is None:
            raise PolicyError(f"uid {uid} is not managed")
        try:
            profile = profiles.get(profile_key)
        except KeyError as e:
            raise PolicyError(str(e)) from None

        user.mode = profile.mode
        user.blocked_categories = list(profile.blocked_categories)
        user.media_level = profile.media_level
        user.language_filter = profile.language_filter
        user.youtube = dict(profile.youtube)
        user.can_install_apps = profile.can_install_apps
        self._save_and_apply()
        log.info("applied profile %s to uid %d", profile_key, uid)
        return None

    def impl_ListCategories(self):
        from . import categories as categories_mod

        bundle = categories_mod.load_any()
        return GLib.Variant("(s)", (json.dumps({
            "version": bundle.version,
            "source": bundle.source,
            "domains": len(bundle),
            "categories": [
                {"name": name,
                 "label": categories_mod.CATEGORY_LABELS.get(name, name)}
                for name in sorted(bundle.categories)
            ],
        }),))

    def impl_SetBlockedCategories(self, uid: int, cats: list[str], _guardian_pw: str):
        user = self.policy.user(uid)
        if user is None:
            raise PolicyError(f"uid {uid} is not managed")
        user.blocked_categories = sorted(set(cats))
        self._save_and_apply()
        return None

    def impl_SetUrlRules(self, uid: int, rules_json: str, _guardian_pw: str):
        from .urlrules import parse_rules

        user = self.policy.user(uid)
        if user is None:
            raise PolicyError(f"uid {uid} is not managed")
        try:
            raw = json.loads(rules_json)
        except ValueError as e:
            raise PolicyError(f"rules are not valid JSON: {e}") from e
        parse_rules(raw)  # reject bad patterns before they reach the proxy
        user.rules = raw
        self._save_and_apply()
        return None

    @staticmethod
    def _new_user(uid: int, username: str, mode: str) -> UserPolicy:
        """A new account's starting settings.

        `mode` may name a ready-made profile instead of a bare filter mode,
        which is what the admin app sends: creating an account and then
        setting eight things one at a time is how accounts end up half
        configured.
        """
        from . import profiles as profiles_mod

        if mode in profiles_mod.BY_KEY:
            profile = profiles_mod.get(mode)
            return UserPolicy(
                uid=uid, username=username, mode=profile.mode,
                blocked_categories=list(profile.blocked_categories),
                media_level=profile.media_level,
                language_filter=profile.language_filter,
                youtube=dict(profile.youtube),
                can_install_apps=profile.can_install_apps)
        return UserPolicy(uid=uid, username=username, mode=mode,
                          blocked_categories=list(_default_categories(mode)))

    def impl_CreateUser(self, username: str, full_name: str, mode: str):
        import pwd

        from . import profiles as profiles_mod

        if mode not in MODES and mode not in profiles_mod.BY_KEY:
            raise PolicyError(f"unknown mode or profile {mode!r}")
        try:
            existing_uid = pwd.getpwnam(username).pw_uid
        except KeyError:
            pass
        else:
            if self.policy.user(existing_uid) is not None:
                raise PolicyError(f"'{username}' already exists and is already managed")
            raise PolicyError(
                f"'{username}' already exists — use Adopt Existing User to manage it"
            )
        uid = self._accounts_create_user(username, full_name)
        self.policy.users.append(self._new_user(uid, username, mode))
        self._save_and_apply()
        return GLib.Variant("(i)", (uid,))

    def impl_SetGuestConfig(self, enabled: bool, mode: str, whitelist: list[str], _guardian_pw: str):
        import pwd
        import shutil

        from . import profiles as profiles_mod

        # A profile key rather than a bare mode, so a guest can be set up
        # in one choice like anybody else. Without this the guest was the
        # one account with no picture, language or YouTube settings at
        # all — a hole in exactly the account nobody is watching.
        if mode in profiles_mod.BY_KEY:
            profile = profiles_mod.get(mode)
            g = self.policy.guest
            g.mode = profile.mode
            g.blocked_categories = list(profile.blocked_categories)
            g.media_level = profile.media_level
            g.language_filter = profile.language_filter
            g.youtube = dict(profile.youtube)
            mode = profile.mode
        elif mode not in MODES:
            raise PolicyError(f"unknown mode or profile {mode!r}")
        g = self.policy.guest
        if enabled:
            try:
                uid = pwd.getpwnam(policy_mod.GUEST_USERNAME).pw_uid
            except KeyError:
                res = subprocess.run(
                    ["useradd", "-m", "-c", "Guest", policy_mod.GUEST_USERNAME],
                    capture_output=True, text=True,
                )
                if res.returncode != 0:
                    raise PolicyError(f"could not create guest account: {res.stderr.strip()}")
                uid = pwd.getpwnam(policy_mod.GUEST_USERNAME).pw_uid
            # Passwordless login; data is wiped after every sign-out by the
            # GDM PostSession hook, and right now for a clean start.
            subprocess.run(["passwd", "-d", policy_mod.GUEST_USERNAME], capture_output=True)
            subprocess.run(["passwd", "-u", policy_mod.GUEST_USERNAME], capture_output=True)
            home = Path(f"/home/{policy_mod.GUEST_USERNAME}")
            if home.exists():
                shutil.rmtree(home)
            subprocess.run(["mkhomedir_helper", policy_mod.GUEST_USERNAME], capture_output=True)
            g.uid = uid
        else:
            subprocess.run(["passwd", "-l", policy_mod.GUEST_USERNAME], capture_output=True)
        g.enabled = enabled
        g.mode = mode
        g.whitelist = sorted(set(whitelist))
        self._save_and_apply()
        return None

    def impl_AdoptUser(self, username: str, mode: str):
        """Bring an EXISTING system user under filter management."""
        import pwd

        from . import profiles as profiles_mod

        if mode not in MODES and mode not in profiles_mod.BY_KEY:
            raise PolicyError(f"unknown mode or profile {mode!r}")
        try:
            uid = pwd.getpwnam(username).pw_uid
        except KeyError:
            raise PolicyError(f"no such user {username!r}") from None
        if uid < 1000:
            raise PolicyError("cannot manage system accounts")
        if self.policy.user(uid) is not None:
            raise PolicyError(f"{username} is already managed")
        self.policy.users.append(self._new_user(uid, username, mode))
        self._save_and_apply()
        return GLib.Variant("(i)", (uid,))

    def impl_SetUserAdmin(self, uid: int, admin: bool, _guardian_pw: str):
        """Make an account an administrator, or stop it being one.

        There was no way to do this at all: the first admin is created
        during setup and a second parent could never be made one
        afterwards, which is a strange thing for a product whose whole
        model is two parents sharing control.

        Guardian-gated, because promoting somebody is a filter change with
        an extra step — an administrator can change every other setting.
        """
        user = self._managed(uid)
        if not admin and user.admin:
            others = [u for u in self.policy.users if u.admin and u.uid != uid]
            if not others:
                # Nobody left who could undo it, including this. The
                # machine would need reinstalling.
                raise PolicyError(
                    "this is the only administrator; make somebody else an "
                    "administrator first")
        user.admin = bool(admin)
        action = "-aG" if admin else "-rG"
        result = subprocess.run(["usermod", action, "kosher-admin",
                                 user.username], capture_output=True, text=True)
        if result.returncode != 0:
            raise PolicyError(
                f"could not change group membership: {result.stderr.strip()}")
        self._save_and_apply()
        log.info("%s is %san administrator", user.username,
                 "" if admin else "no longer ")
        return None

    def impl_RemoveUser(self, uid: int):
        user = self.policy.user(uid)
        if user is None:
            raise PolicyError(f"uid {uid} is not managed")
        if user.admin and not [u for u in self.policy.users
                               if u.admin and u.uid != uid]:
            raise PolicyError(
                "this is the only administrator; the computer would have "
                "nobody who could change anything")
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
        # A fresh accountsservice user has a LOCKED password, and GDM hides
        # locked accounts. "Set password at first login" (what GNOME Settings
        # does) unlocks it, shows it on the login screen, and lets the person
        # choose their own password on first sign-in.
        self.connection.call_sync(
            "org.freedesktop.Accounts", path,
            "org.freedesktop.Accounts.User", "SetPasswordMode",
            GLib.Variant("(i)", (1,)),  # 1 = SET_AT_LOGIN
            None, Gio.DBusCallFlags.NONE, -1, None,
        )
        uid = self.connection.call_sync(
            "org.freedesktop.Accounts", path,
            "org.freedesktop.DBus.Properties", "Get",
            GLib.Variant("(ss)", ("org.freedesktop.Accounts.User", "Uid")),
            GLib.VariantType("(v)"), Gio.DBusCallFlags.NONE, -1, None,
        ).unpack()[0]  # (v) unpacks recursively -> plain int
        return int(uid)

    def _accounts_delete_user(self, uid: int) -> None:
        self.connection.call_sync(
            "org.freedesktop.Accounts", "/org/freedesktop/Accounts",
            "org.freedesktop.Accounts", "DeleteUser",
            GLib.Variant("(xb)", (uid, True)),
            None, Gio.DBusCallFlags.NONE, -1, None,
        )

    # ---- Apps ------------------------------------------------------------

    def impl_ListCatalog(self):
        return GLib.Variant("(s)", (json.dumps(apps.load_catalog()),))

    def impl_ListInstalled(self):
        return GLib.Variant("(as)", (sorted(apps.installed_refs()),))

    def impl_ListInstalledDetails(self):
        return GLib.Variant("(s)", (json.dumps(apps.installed_details()),))

    def impl_SetUserApps(self, uid: int, refs: list[str]):
        """Restrict which installed apps a user may run (empty = all)."""
        user = self.policy.user(uid)
        if user is None:
            raise PolicyError(f"uid {uid} is not managed")
        user.apps = sorted(set(refs))
        self._save_and_apply()  # re-applies malcontent filters
        return None

    def impl_InstallApp(self, ref: str, *, _uid: int):
        import pwd

        user = self.policy.user(_uid)
        if _uid != 0 and user is not None and not user.can_install_apps:
            raise PolicyError("app installation is turned off for this user")
        self.app_manager.install(ref)  # validates against the allowlist
        try:
            username = pwd.getpwuid(_uid).pw_name
        except KeyError:
            username = str(_uid)
        apps.record_install(ref, _uid, username)
        return None

    def impl_RemoveApp(self, ref: str):
        self.app_manager.remove(ref)
        apps.forget_install(ref)
        return None

    def impl_SearchApps(self, query: str):
        return GLib.Variant("(s)", (json.dumps(apps.search_remote(query)),))

    def impl_ApproveApp(self, ref: str, name: str, summary: str):
        apps.approve(ref, name, summary)
        return None

    def impl_UnapproveApp(self, ref: str):
        apps.unapprove(ref)
        return None

    def impl_SetUserCanInstall(self, uid: int, can_install: bool):
        user = self.policy.user(uid)
        if user is None:
            raise PolicyError(f"uid {uid} is not managed")
        user.can_install_apps = can_install
        self._save_and_apply()
        return None

    # -- app job callbacks (worker threads -> main loop -> D-Bus signals) --

    def _emit_app_signal(self, name: str, signature: str, args: tuple) -> None:
        def emit():
            if self.connection:
                self.connection.emit_signal(
                    None, OBJECT_PATH, "org.kosherlinux.Daemon1.Apps", name,
                    GLib.Variant(signature, args),
                )
            return False

        GLib.idle_add(emit)

    def _on_app_progress(self, ref: str, percent: int, status: str) -> None:
        self._emit_app_signal("AppProgress", "(sis)", (ref, percent, status))

    def _on_app_finished(self, ref: str, ok: bool, error: str) -> None:
        if ok:
            GLib.idle_add(lambda: (self._apply_mct(), False)[1])
        self._emit_app_signal("AppFinished", "(sbs)", (ref, ok, error))

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

    # ---- First-boot setup ------------------------------------------------

    def setup_complete(self) -> bool:
        """Setup is finished only when FinishSetup has stamped it.

        Deliberately NOT "an admin exists": the wizard creates the admin and
        then still has to set the guardian and boot passwords, and a wizard
        interrupted midway must be able to resume. CreateFirstAdmin carries
        its own guard against a second admin.
        """
        return SETUP_STAMP.exists()

    def impl_IsComplete(self):
        return GLib.Variant("(b)", (self.setup_complete(),))

    def impl_CreateFirstAdmin(self, username: str, full_name: str, password: str):
        import pwd

        if any(u.admin for u in self.policy.users):
            raise PolicyError("an administrator account already exists")
        if len(password) < 6:
            raise PolicyError("password must be at least 6 characters")
        try:
            pwd.getpwnam(username)
        except KeyError:
            pass
        else:
            raise PolicyError(f"'{username}' already exists")

        uid = self._accounts_create_user(username, full_name)
        # The admin sets a real password here (not SET_AT_LOGIN): they will
        # need it immediately for polkit prompts.
        res = subprocess.run(["chpasswd"], input=f"{username}:{password}",
                             capture_output=True, text=True)
        if res.returncode != 0:
            raise PolicyError(f"could not set password: {res.stderr.strip()}")
        subprocess.run(["usermod", "-aG", "kosher-admin", username], check=False)

        self.policy.users.append(UserPolicy(
            uid=uid, username=username, mode="filtered", admin=True))
        self._save_and_apply()
        log.info("first admin created: %s (uid %d)", username, uid)
        return GLib.Variant("(i)", (uid,))

    def impl_FinishSetup(self, guardian_password: str, grub_password: str):
        if not any(u.admin for u in self.policy.users):
            raise PolicyError("create the administrator account first")
        if guardian_password:
            self.guardian.set_password(guardian_password)
            self.policy.guardian_enabled = True
            self._save_and_apply()
        if grub_password:
            self._set_grub_password(grub_password)
        SETUP_STAMP.parent.mkdir(parents=True, exist_ok=True)
        SETUP_STAMP.write_text("1\n")
        subprocess.run(["systemctl", "disable", "kosher-firstboot.service"],
                       capture_output=True)
        log.info("initial setup complete")
        return None

    @staticmethod
    def _set_grub_password(password: str) -> None:
        """Password-protect the boot menu (blocks kernel-argument edits)."""
        res = subprocess.run(["grub2-mkpasswd-pbkdf2"], text=True,
                             input=f"{password}\n{password}\n", capture_output=True)
        if res.returncode != 0:
            raise PolicyError(f"could not hash the boot password: {res.stderr.strip()}")
        digest = next((line.split()[-1] for line in res.stdout.splitlines()
                       if "grub.pbkdf2" in line), None)
        if digest is None:
            raise PolicyError("unexpected grub2-mkpasswd-pbkdf2 output")
        GRUB_USER_CFG.parent.mkdir(parents=True, exist_ok=True)
        GRUB_USER_CFG.write_text(
            f"GRUB2_PASSWORD={digest}\n")
        GRUB_USER_CFG.chmod(0o600)

    # ---- Portal ----------------------------------------------------------

    def impl_Enrol(self, portal_url: str, code: str, _guardian_pw: str):
        from . import sync

        enrolment = sync.enrol(portal_url, code)
        enrolment.save()
        log.info("enrolled with %s as device %s", portal_url, enrolment.device_id)
        self.sync_now()
        return GLib.Variant("(s)", (enrolment.device_id,))

    def impl_Unenrol(self, _guardian_pw: str):
        from . import sync

        sync.ENROLMENT_PATH.unlink(missing_ok=True)
        log.info("unenrolled from the portal; local policy stands")
        return None

    def impl_SyncNow(self):
        return GLib.Variant("(b)", (self.sync_now(),))

    def impl_PortalStatus(self):
        from . import sync

        enrolment = sync.Enrolment.load()
        return GLib.Variant("(s)", (json.dumps({
            "enrolled": enrolment is not None,
            "portal_url": enrolment.portal_url if enrolment else "",
            "device_id": enrolment.device_id if enrolment else "",
            "revision": self.policy.revision,
            "source": self.policy.source,
        }),))

    def sync_now(self) -> bool:
        """Pull signed updates from the portal. True if anything was applied."""
        from . import lists, sync

        enrolment = sync.Enrolment.load()
        if enrolment is None:
            raise PolicyError("this device is not enrolled with a portal")
        client = sync.PortalClient(enrolment)

        # Lists first, and separately: they change when the web changes,
        # not when a parent changes their mind, and a failure to reach one
        # must not stop the other from arriving.
        changed = False
        try:
            bundle = client.fetch_lists(lists.portal_version())
        except sync.SyncError as e:
            log.warning("could not fetch filter lists: %s", e)
        else:
            if bundle is not None:
                written = lists.install_portal_lists(
                    bundle.get("lists") or {}, bundle.get("version", 0))
                log.info("installed portal lists v%s: %s",
                         bundle.get("version"), ", ".join(written) or "none")
                changed = bool(written)
                # The catalogue rides in the same signed document but not
                # in its body: 190 MB of database is a URL and a hash, and
                # the signature over the hash is what makes the download
                # need no trust of its own.
                manifest = bundle.get("catalog")
                if manifest:
                    from . import catalogsync

                    try:
                        count = catalogsync.update(manifest)
                    except catalogsync.CatalogError as e:
                        log.error("catalogue update refused: %s", e)
                    else:
                        if count:
                            log.info("catalogue now holds %d domains", count)
                            changed = True

        doc = client.fetch_policy(self.policy.revision)
        if doc is None:
            return changed
        incoming = Policy.from_dict(doc)
        incoming.source = "portal"
        self.policy = incoming
        # Keep the revision the portal issued: it is what replay protection
        # compares against next time.
        policy_mod.save(self.policy, bump_revision=False)
        apply_policy(self.policy)
        self._apply_mct()
        log.info("applied portal policy revision %d", self.policy.revision)
        return True

    # ---- Session ---------------------------------------------------------

    def impl_Unlock(self, *, _uid: int):
        self.sessions.unlock(_uid)
        log.info("admin session opened for uid %d", _uid)
        return None

    def impl_Lock(self, *, _uid: int):
        self.sessions.lock(_uid)
        return None

    def impl_Status(self, *, _uid: int):
        return GLib.Variant("(bib)", (
            self.sessions.is_unlocked(_uid),
            int(self.sessions.seconds_remaining(_uid)),
            not self.policy.guardian_enabled or self.sessions.has_guardian(_uid),
        ))

    def impl_VerifyGuardian(self, password: str, *, _uid: int):
        if not self.policy.guardian_enabled:
            return None
        if not self.guardian.verify(password):
            raise GuardianError("guardian password incorrect")
        self.sessions.grant_guardian(_uid)
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
