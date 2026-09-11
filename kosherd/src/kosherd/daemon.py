"""kosherd — the privileged management daemon.

Runs as root, owns org.kosherlinux.Daemon1 on the system bus. Every method is
gated by polkit (see auth.py); filter-weakening methods additionally require
the guardian password when guardian mode is enabled. The daemon is the ONLY
writer of enforcement state — admin app, kosherctl, and the future portal sync
agent are all just D-Bus clients of it.
"""

from __future__ import annotations

import copy
import errno
import json
import logging
import re
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
    <method name="GetMySettings">
      <arg direction="out" type="s" name="settings_json"/>
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
    <method name="AllowUrl">
      <arg direction="in" type="i" name="uid"/>
      <arg direction="in" type="s" name="url"/>
      <arg direction="in" type="b" name="whole_site"/>
      <arg direction="in" type="s" name="guardian_password"/>
    </method>
    <method name="ListActivity">
      <arg direction="in" type="i" name="since"/>
      <arg direction="in" type="i" name="uid"/>
      <arg direction="out" type="s" name="events_json"/>
    </method>
    <method name="ActivitySummary">
      <arg direction="out" type="s" name="summary_json"/>
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
    <method name="SetLayout">
      <arg direction="in" type="i" name="uid"/>
      <arg direction="in" type="s" name="layout"/>
    </method>
    <method name="SetCoverStyle">
      <arg direction="in" type="i" name="uid"/>
      <arg direction="in" type="s" name="style"/>
    </method>
    <method name="SetAdBlock">
      <arg direction="in" type="b" name="enabled"/>
      <arg direction="in" type="s" name="guardian_password"/>
    </method>
    <method name="GetMyLayout">
      <arg direction="out" type="s" name="layout"/>
    </method>
    <method name="ApplyProfile">
      <arg direction="in" type="i" name="uid"/>
      <arg direction="in" type="s" name="profile"/>
      <arg direction="in" type="s" name="guardian_password"/>
    </method>
    <method name="ListProfiles">
      <arg direction="out" type="s" name="profiles_json"/>
    </method>
    <method name="SaveProfile">
      <arg direction="in" type="i" name="uid"/>
      <arg direction="in" type="s" name="label"/>
      <arg direction="in" type="s" name="description"/>
      <arg direction="in" type="s" name="guardian_password"/>
      <arg direction="out" type="s" name="key"/>
    </method>
    <method name="DeleteProfile">
      <arg direction="in" type="s" name="key"/>
      <arg direction="in" type="s" name="guardian_password"/>
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
    <method name="DeploymentStatus">
      <arg direction="out" type="s" name="status_json"/>
    </method>
    <method name="Rollback"/>
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
    <method name="AdminExists">
      <arg direction="out" type="b" name="exists"/>
      <arg direction="out" type="s" name="username"/>
    </method>
    <method name="ExistingAccounts">
      <arg direction="out" type="as" name="usernames"/>
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
        # The policy as it was before this call. A method that changes the
        # in-memory policy and then fails (a save refused by the schema, a
        # service that would not restart) must not leave the change behind:
        # it did once, and every later call failed on the same stale user
        # until the daemon was restarted. On any error the snapshot is put
        # back, so a failed call is a call that did not happen.
        snapshot = None
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
            snapshot = copy.deepcopy(self.policy)
            result = getattr(self, f"impl_{method}")(*args, _uid=uid) \
                if method in UID_AWARE else getattr(self, f"impl_{method}")(*args)
            invocation.return_value(result)
            log.info("%s by %s (uid %s): ok", method, sender, uid)
            if method in access.CHANGES:
                self._note_change(method, args, by=uid)
        except (auth.NotAuthorized, GuardianError, PolicyError, AppError, KeyError, ValueError) as e:
            self._roll_back(snapshot, method)
            log.warning("%s by %s refused: %s", method, sender, e)
            invocation.return_dbus_error(ERROR_NAME, str(e))
        except GLib.Error as e:
            # A system service we called (accountsservice, polkit, ...) said no —
            # pass its message through instead of masking it as 'internal error'.
            self._roll_back(snapshot, method)
            log.warning("%s by %s failed downstream: %s", method, sender, e.message)
            invocation.return_dbus_error(ERROR_NAME, e.message)
        except Exception as e:  # noqa: BLE001 - daemon must not crash on a bad call
            self._roll_back(snapshot, method)
            log.exception("%s failed", method)
            invocation.return_dbus_error(ERROR_NAME, f"internal error: {e}")

    def _roll_back(self, snapshot, method: str) -> None:
        """Put the in-memory policy back to what it was before a failed call."""
        if snapshot is None or snapshot == self.policy:
            return
        self.policy = snapshot
        log.warning("%s failed part-way; the policy was put back", method)

    def _note_change(self, method: str, args: list, *, by: int) -> None:
        """One line in the activity log saying who changed what.

        In a household with two admins and a guardian password, "who set
        this?" is a real question. Passwords never go in: the trailing
        guardian argument of a gated method is dropped, and the guardian
        methods log no arguments at all.
        """
        from . import activity

        try:
            kept = list(args)
            if method in access.GUARDIAN_GATED and kept:
                kept = kept[:-1]
            if method in access.NO_ARGS_LOGGED:
                kept = []
            target = kept[0] if kept and isinstance(kept[0], int) \
                and not isinstance(kept[0], bool) and method in access.PER_USER else -1
            activity.record("kosherd", activity.CHANGE, target, by=by,
                            method=method, args=kept,
                            guardian=self.policy.guardian_enabled
                            and method in access.GUARDIAN_GATED)
        except Exception:  # noqa: BLE001 - the log is a convenience
            log.debug("could not record a change", exc_info=True)

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
        # Entering a filtering mode with nothing to filter is the trap the
        # defaults exist to prevent; an account switched from "unfiltered"
        # has an empty list and would otherwise stay wide open.
        if not user.blocked_categories:
            user.blocked_categories = list(_default_categories(mode))
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
        status["problems"] = selfcheck.problems(found) + \
            selfcheck.empty_accounts(self.policy.effective_users())

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
        host = self._grant(user, document["url"], whole_site)
        log.info("approved access for uid %d to %s", user.uid, host)
        return None

    def impl_AllowUrl(self, uid: int, url: str, whole_site: bool,
                      _guardian_pw: str):
        """Allow a page the filter blocked, straight from the activity
        view — the same grant as approving a request, without waiting for
        the person to ask."""
        user = self.policy.user(uid)
        if user is None:
            raise PolicyError(f"uid {uid} is not managed")
        host = self._grant(user, url, whole_site)
        log.info("allowed uid %d to %s from the activity log", uid, host)
        return None

    def _grant(self, user, url: str, whole_site: bool) -> str:
        """Open one page or one site for an account, in the terms of its
        mode: a whitelist entry, or an allow rule ahead of the rest."""
        from urllib.parse import urlsplit

        parts = urlsplit(url)
        host = (parts.hostname or "").lower().strip(".")
        if not host:
            raise PolicyError("that request has no address")

        if user.mode == "whitelist":
            if host not in user.whitelist:
                user.whitelist = sorted({*user.whitelist, host})
        else:
            pattern = host if whole_site else _url_pattern(url)
            if not any(r.get("pattern") == pattern and r.get("action") == "allow"
                       for r in user.rules):
                # Ahead of the existing rules: a rule added after the one
                # that blocked the page would never be reached.
                user.rules = [{"action": "allow", "pattern": pattern},
                              *user.rules]
        self._save_and_apply()
        return host

    def impl_ListActivity(self, since: int, uid: int):
        """What the filter did, newest first. uid -1 means everyone.

        Usernames ride along so the app never has to join; an account that
        has since been removed shows as "?" rather than vanishing, because
        what was blocked for it still happened.
        """
        from . import activity

        self._trim_activity()
        names = {u.uid: u.username for u in self.policy.users}
        guest_uid = getattr(self.policy.guest, "uid", None)
        if guest_uid is not None:
            names.setdefault(guest_uid, "Guest")
        found = []
        for document in activity.events(since, None if uid < 0 else uid):
            entry = dict(document)
            entry["username"] = names.get(document["uid"], "?")
            if document["kind"] == activity.CHANGE:
                entry["by_username"] = names.get(document.get("by"), "?")
            found.append(entry)
        return GLib.Variant("(s)", (json.dumps(found),))

    def impl_ActivitySummary(self):
        """Today's counts per account, for the cards on the family board."""
        from . import activity

        found = activity.events(activity.day_start())
        counts = {str(uid): entry
                  for uid, entry in activity.summary(found).items()}
        return GLib.Variant("(s)", (json.dumps(counts),))

    _activity_trimmed_at = 0.0

    def _trim_activity(self) -> None:
        import time

        from . import activity

        now = time.monotonic()
        if now - self._activity_trimmed_at < 3600:
            return
        self._activity_trimmed_at = now
        activity.trim()

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

    def impl_SetLayout(self, uid: int, layout: str):
        """Choose how an account's desktop is laid out.

        Not guardian-gated and saved without re-rendering enforcement: the
        filter is per-uid at the network layer and does not care which
        shell draws the windows. What does change is which session the
        login screen offers first — the advanced layout is a separate
        session — so the account's default session is updated alongside.
        """
        from .policy import LAYOUTS

        if layout not in LAYOUTS:
            raise PolicyError(f"unknown layout {layout!r}")
        user = self._managed(uid)
        previous = user.layout
        user.layout = layout
        try:
            self._save_only()
        except Exception:
            user.layout = previous
            raise
        self._accounts_set_session(uid, layout)
        log.info("uid %d desktop layout -> %s", uid, layout)
        return None

    def impl_SetCoverStyle(self, uid: int, style: str):
        """How a kept-but-partly-hidden picture is covered for this account.

        Cosmetic — what is judged and when is the media level — so no
        guardian gate; but the proxy reads it from the rendered rules, so
        it is saved AND applied.
        """
        from .policy import COVER_STYLES

        if style not in COVER_STYLES:
            raise PolicyError(f"unknown cover style {style!r}")
        self._managed(uid).cover_style = style
        self._save_and_apply()
        return None

    def impl_SetAdBlock(self, enabled: bool, _guardian_pw: str):
        """Ads and trackers blocked at the resolvers for every account.

        Machine-wide, like a Pi-hole: all DNS on this machine is forced
        through its own resolvers, so this reaches every browser and app.
        Switching it OFF is what the guardian gate protects — an ad network
        is also where immodest imagery arrives uninvited.
        """
        self.policy.adblock = bool(enabled)
        self._save_and_apply()
        return None

    def impl_GetMyLayout(self, _uid: int):
        """The caller's own layout, for the sign-in helper.

        Any active local user may ask; an unmanaged account gets the
        default. The one setting a non-admin may read about themselves,
        and it says nothing about how they are filtered.
        """
        from .policy import DEFAULT_LAYOUT

        user = self.policy.user(_uid)
        layout = user.layout if user is not None else DEFAULT_LAYOUT
        return GLib.Variant("(s)", (layout,))

    def impl_GetMySettings(self, _uid: int):
        """What applies to the CALLER's own account, read-only.

        The uid comes from the D-Bus connection, never from an argument, so
        this cannot be pointed at somebody else's account. Nothing here
        describes another user, the guardian, or the machine's current
        health — only the rules this person is living under.

        An unmanaged account (no policy entry) is reported honestly as
        unmanaged rather than being given a fabricated default, because
        "this account is not filtered" is exactly the thing someone needs
        to be told plainly.
        """
        from . import categories

        user = self.policy.user(_uid)
        if user is None:
            return GLib.Variant("(s)", (json.dumps({
                "managed": False,
                "adblock": self.policy.adblock,
            }),))

        settings = {
            "managed": True,
            "username": user.username,
            "mode": user.mode,
            "admin": user.admin,
            "can_install_apps": user.can_install_apps,
            "language_filter": user.language_filter,
            "adblock": self.policy.adblock,
            "blocked_categories": [
                {"key": key, "label": categories.CATEGORY_LABELS[key]}
                for key in user.blocked_categories
                if key in categories.CATEGORY_LABELS
            ],
        }
        # Only meaningful where pictures are actually judged; showing "all
        # pictures allowed" to a whitelist account would be misleading,
        # since the whitelist is what governs there.
        if user.mode in INSPECTED_MODES:
            settings["media_level"] = user.media_level
            settings["youtube"] = user.youtube
        if user.mode == "whitelist":
            # The whitelist is already deliberately discoverable through
            # KosherOS Search, so listing it here reveals nothing new and
            # answers the question this app exists to answer.
            settings["whitelist"] = sorted(user.whitelist)
        return GLib.Variant("(s)", (json.dumps(settings),))

    def impl_ListProfiles(self):
        from . import profiles

        described = profiles.describe(self.policy.custom_profiles)
        for entry in described:
            entry["default"] = entry["key"] == profiles.DEFAULT_PROFILE
        return GLib.Variant("(s)", (json.dumps(described),))

    def impl_SaveProfile(self, uid: int, label: str, description: str,
                         _guardian_pw: str):
        """Snapshot an account's current settings as a named preset.

        The everyday path to a good preset: tune one child's account until
        it is right, save it, apply it to the others. Saving the same label
        again replaces that preset.
        """
        from . import profiles

        user = self.policy.user(uid)
        if user is None:
            raise PolicyError(f"uid {uid} is not managed")
        if not label.strip():
            raise PolicyError("a preset needs a name")
        if len(label.strip()) > 60:
            raise PolicyError("keep the preset name under 60 characters")
        preset = profiles.from_user(user, label, description)
        if preset.key in profiles.BY_KEY:
            raise PolicyError(f"'{label}' is a built-in profile; pick another name")
        previous = list(self.policy.custom_profiles)
        self.policy.custom_profiles = [
            c for c in previous if c.get("key") != preset.key
        ] + [profiles.to_dict(preset)]
        try:
            self._save_only()
        except Exception:
            # Never leave memory ahead of disk: a failed save (schema,
            # disk) must not make the daemon believe in a preset the
            # policy file does not hold.
            self.policy.custom_profiles = previous
            raise
        log.info("saved preset %s from uid %d", preset.key, uid)
        return GLib.Variant("(s)", (preset.key,))

    def impl_DeleteProfile(self, key: str, _guardian_pw: str):
        from . import profiles

        if not key.startswith(profiles.CUSTOM_PREFIX):
            raise PolicyError("built-in profiles cannot be deleted")
        previous = list(self.policy.custom_profiles)
        remaining = [c for c in previous if c.get("key") != key]
        if len(remaining) == len(previous):
            raise PolicyError(f"no preset {key!r}")
        self.policy.custom_profiles = remaining
        try:
            self._save_only()
        except Exception:
            self.policy.custom_profiles = previous
            raise
        return None

    def _save_only(self) -> None:
        """Persist and announce a policy change that alters no enforcement
        (a preset saved or deleted): no ruleset render, no proxy restart."""
        policy_mod.save(self.policy)
        if self.connection:
            self.connection.emit_signal(
                None, OBJECT_PATH, "org.kosherlinux.Daemon1.Profiles", "PolicyChanged",
                GLib.Variant("(i)", (self.policy.revision,)),
            )

    def impl_ApplyProfile(self, uid: int, profile_key: str, _guardian_pw: str):
        from . import profiles

        user = self.policy.user(uid)
        if user is None:
            raise PolicyError(f"uid {uid} is not managed")
        try:
            profile = profiles.get(profile_key, self.policy.custom_profiles)
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
        present = bundle.categories
        return GLib.Variant("(s)", (json.dumps({
            "version": bundle.version,
            "source": bundle.source,
            "domains": len(bundle),
            # Only the user-facing set, in its defined order — never the raw
            # catalogue buckets. A labelled category with no domains in this
            # catalogue is still offered (a future or partial catalogue may
            # fill it); the UI can note it is empty.
            # Alphabetical by the label the person actually reads, so the
            # toggle list is scannable rather than in an internal order.
            "categories": [
                {"name": name,
                 "label": categories_mod.CATEGORY_LABELS[name],
                 "present": name in present}
                for name in sorted(
                    categories_mod.USER_FACING_CATEGORIES,
                    key=lambda n: categories_mod.CATEGORY_LABELS[n].lower())
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
    def _new_user(uid: int, username: str, mode: str, custom=()) -> UserPolicy:
        """A new account's starting settings.

        `mode` may name a ready-made profile instead of a bare filter mode,
        which is what the admin app sends: creating an account and then
        setting eight things one at a time is how accounts end up half
        configured.
        """
        from . import profiles as profiles_mod

        if mode in {p.key for p in profiles_mod.all_profiles(custom)}:
            profile = profiles_mod.get(mode, custom)
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

        if mode not in MODES and mode not in {
                p.key for p in profiles_mod.all_profiles(self.policy.custom_profiles)}:
            raise PolicyError(f"unknown mode or profile {mode!r}")
        # Everything that can be checked is checked BEFORE the account exists.
        # The account used to be created first and refused by the policy
        # afterwards, which left a user on the machine that nothing managed.
        if not re.fullmatch(policy_mod.USERNAME_PATTERN, username or ""):
            raise PolicyError(f"'{username}' is not a valid username: {policy_mod.USERNAME_RULE}")
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
        try:
            self.policy.users.append(
                self._new_user(uid, username, mode, self.policy.custom_profiles))
            self._save_and_apply()
        except Exception:
            # The policy would not take the account: undo the half we did,
            # so the machine is exactly as it was before the click.
            log.warning("could not manage new account %s; removing it again", username)
            try:
                self._accounts_delete_user(uid)
            except Exception:  # noqa: BLE001 - report the original failure
                log.exception("could not remove the half-created account %s", username)
            raise
        return GLib.Variant("(i)", (uid,))

    def impl_SetGuestConfig(self, enabled: bool, mode: str, whitelist: list[str], _guardian_pw: str):
        import pwd
        import shutil

        from . import profiles as profiles_mod

        # A profile key rather than a bare mode, so a guest can be set up
        # in one choice like anybody else. Without this the guest was the
        # one account with no picture, language or YouTube settings at
        # all — a hole in exactly the account nobody is watching.
        custom = self.policy.custom_profiles
        if mode in {p.key for p in profiles_mod.all_profiles(custom)}:
            profile = profiles_mod.get(mode, custom)
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

        if mode not in MODES and mode not in {
                p.key for p in profiles_mod.all_profiles(self.policy.custom_profiles)}:
            raise PolicyError(f"unknown mode or profile {mode!r}")
        try:
            uid = pwd.getpwnam(username).pw_uid
        except KeyError:
            raise PolicyError(f"no such user {username!r}") from None
        if uid < 1000:
            raise PolicyError("cannot manage system accounts")
        if self.policy.user(uid) is not None:
            raise PolicyError(f"{username} is already managed")
        self.policy.users.append(self._new_user(uid, username, mode, self.policy.custom_profiles))
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

    # Which login-screen session each layout lives in. The classic and
    # tiling layouts are both GNOME (the difference is applied inside the
    # session by kosher-layout); the advanced layout is its own session.
    LAYOUT_SESSIONS = {"classic": "gnome", "tiling": "gnome", "advanced": "niri"}

    def _accounts_set_session(self, uid: int, layout: str) -> None:
        """Make the login screen default this account to the right session.

        GDM remembers a per-user session in accountsservice and preselects
        it; without this the person who was given the advanced layout would
        have to find the gear menu on the login screen and know what to pick.
        A failure here is logged, not raised: the layout is saved already
        and the session can still be picked by hand.
        """
        session = self.LAYOUT_SESSIONS[layout]
        if self.connection is None:
            return
        try:
            path = self.connection.call_sync(
                "org.freedesktop.Accounts", "/org/freedesktop/Accounts",
                "org.freedesktop.Accounts", "FindUserById",
                GLib.Variant("(x)", (uid,)),
                GLib.VariantType("(o)"), Gio.DBusCallFlags.NONE, -1, None,
            )[0]
            for method, value in (("SetSessionType", "wayland"),
                                  ("SetSession", session)):
                self.connection.call_sync(
                    "org.freedesktop.Accounts", path,
                    "org.freedesktop.Accounts.User", method,
                    GLib.Variant("(s)", (value,)),
                    None, Gio.DBusCallFlags.NONE, -1, None,
                )
        except GLib.Error as e:
            log.warning("could not set uid %d's login session to %s: %s",
                        uid, session, e.message)

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

    def impl_DeploymentStatus(self):
        """Which image is booted, and which one "go back" would return to.

        Read defensively: bootc's JSON has moved between releases and this
        must degrade to "unknown" rather than raise, because the one moment
        somebody needs this screen is when the machine is already unwell.
        """
        res = subprocess.run(["bootc", "status", "--json"],
                             capture_output=True, text=True)
        status: dict = {"booted": None, "rollback": None,
                        "rollback_queued": False, "staged": None}
        if res.returncode != 0:
            status["error"] = (res.stderr or res.stdout).strip()
            return GLib.Variant("(s)", (json.dumps(status),))

        def describe(entry) -> dict | None:
            if not isinstance(entry, dict):
                return None
            image = entry.get("image")
            image = image.get("image") if isinstance(image, dict) else None
            ref = image.get("image") if isinstance(image, dict) else None
            outer = entry.get("image")
            version = outer.get("version") if isinstance(outer, dict) else None
            stamp = outer.get("timestamp") if isinstance(outer, dict) else None
            return {"image": ref, "version": version, "timestamp": stamp}

        try:
            doc = json.loads(res.stdout)["status"]
        except (ValueError, KeyError, TypeError) as e:
            status["error"] = f"could not read bootc status: {e}"
            return GLib.Variant("(s)", (json.dumps(status),))

        status["booted"] = describe(doc.get("booted"))
        status["rollback"] = describe(doc.get("rollback"))
        status["staged"] = describe(doc.get("staged"))
        status["rollback_queued"] = bool(doc.get("rollbackQueued", False))
        return GLib.Variant("(s)", (json.dumps(status),))

    def impl_Rollback(self):
        """Go back to the deployment this machine booted before.

        Deliberately NOT guardian-gated, unlike the calls that weaken the
        filter. Two reasons. The image being returned to is one this
        machine already ran and already trusted, so this is not a way to
        reach anything new. And greenboot has to be able to do this with no
        password at all when a new image fails to bring the filter up — so
        gating the manual path would buy very little while risking the
        thing this exists to prevent: a family with a broken computer and
        nobody home who can fix it.
        """
        res = subprocess.run(["bootc", "rollback"], capture_output=True,
                             text=True)
        if res.returncode != 0:
            raise PolicyError(f"bootc rollback failed: {res.stderr.strip()}")
        # The dispatcher writes the activity entry, because Rollback is in
        # access.CHANGES — "who put this machine back?" deserves an answer
        # in the same log as every other change.
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

    def impl_AdminExists(self):
        """Whether the account step is already done, and who it made.

        This is what lets the wizard RESUME. A finish step that failed (the
        boot password could not be written, say) left the admin created but
        setup unstamped; the wizard came back up at the account page and
        insisted on a second account, because it had no way to ask.
        """
        admin = next((u for u in self.policy.users if u.admin), None)
        return GLib.Variant("(bs)", (admin is not None,
                                     admin.username if admin else ""))

    @staticmethod
    def _human_accounts() -> list[str]:
        """Login accounts that exist before setup made any: uid 1000+, a
        real shell, and not one of ours. An installer, a kickstart or a
        dev image may have created one."""
        import pwd

        return sorted(
            e.pw_name for e in pwd.getpwall()
            if 1000 <= e.pw_uid < 60000
            and not e.pw_name.startswith("kosher-")
            and e.pw_shell not in ("/sbin/nologin", "/usr/sbin/nologin", "/bin/false"))

    def impl_ExistingAccounts(self):
        return GLib.Variant("(as)", (self._human_accounts(),))

    def impl_CreateFirstAdmin(self, username: str, full_name: str, password: str):
        import pwd

        if any(u.admin for u in self.policy.users):
            raise PolicyError("an administrator account already exists")
        if len(password) < 6:
            raise PolicyError("password must be at least 6 characters")
        try:
            existing = pwd.getpwnam(username)
        except KeyError:
            existing = None

        if existing is None:
            uid = self._accounts_create_user(username, full_name)
        elif username in self._human_accounts():
            # Adopt it. Refusing here ("already exists") was a dead end: the
            # person had to invent a second account for a computer that
            # already had the one they wanted. The password they type
            # becomes its password; whatever the installer set is gone.
            uid = existing.pw_uid
            log.info("adopting existing account %s (uid %d) as the first admin",
                     username, uid)
        else:
            raise PolicyError(f"'{username}' is a system account; choose another name")
        # The admin sets a real password here (not SET_AT_LOGIN): they will
        # need it immediately for polkit prompts.
        res = subprocess.run(["chpasswd"], input=f"{username}:{password}",
                             capture_output=True, text=True)
        if res.returncode != 0:
            raise PolicyError(f"could not set password: {res.stderr.strip()}")
        subprocess.run(["usermod", "-aG", "kosher-admin", username], check=False)

        # Through _new_user, so the administrator gets the same default
        # category floor as any account. Built directly, this account had
        # NO categories: the first real family test found gambling, dating
        # and VPN sites all open for the admin — "filtered" that filtered
        # nothing, on the one account every machine has.
        admin = self._new_user(uid, username, "filtered")
        admin.admin = True
        self.policy.users.append(admin)
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
        """Password-protect the boot menu (blocks kernel-argument edits).

        Checked against the GRUB source, because getting this wrong locks a
        family out of their own computer: with `superusers` set, GRUB
        restricts an entry to superusers only when the entry has a users
        list. Fedora's blscfg passes users=NULL for a BLS entry with no
        `grub_users` field, and ostree's entries have none — so they still
        BOOT freely; only editing an entry or opening the command line asks
        for this password. Which is exactly the point.
        """
        res = subprocess.run(["grub2-mkpasswd-pbkdf2"], text=True,
                             input=f"{password}\n{password}\n", capture_output=True)
        if res.returncode != 0:
            raise PolicyError(f"could not hash the boot password: {res.stderr.strip()}")
        digest = next((line.split()[-1] for line in res.stdout.splitlines()
                       if "grub.pbkdf2" in line), None)
        if digest is None:
            raise PolicyError("unexpected grub2-mkpasswd-pbkdf2 output")
        # ostree mounts /boot read-only, so a plain write fails with EROFS
        # ("cannot write") and the first person to enable this on a real
        # machine saw exactly that. Remount for the write and put it back.
        # bootupd's static grub.cfg sources ${prefix}/user.cfg and sets
        # prefix to the boot partition's grub2 dir, so this path is the one
        # GRUB actually reads.
        boot = GRUB_USER_CFG.parent.parent
        remounted = False
        try:
            try:
                GRUB_USER_CFG.parent.mkdir(parents=True, exist_ok=True)
                GRUB_USER_CFG.write_text(f"GRUB2_PASSWORD={digest}\n")
            except OSError as e:
                if e.errno != errno.EROFS:
                    raise
                res = subprocess.run(["mount", "-o", "remount,rw", str(boot)],
                                     capture_output=True, text=True)
                if res.returncode != 0:
                    raise PolicyError(
                        f"could not make {boot} writable: {res.stderr.strip()}"
                    ) from e
                remounted = True
                GRUB_USER_CFG.parent.mkdir(parents=True, exist_ok=True)
                GRUB_USER_CFG.write_text(f"GRUB2_PASSWORD={digest}\n")
            GRUB_USER_CFG.chmod(0o600)
        except OSError as e:
            raise PolicyError(
                f"could not write the boot password to {GRUB_USER_CFG}: {e}. "
                "Turn the boot menu password off for now and set it later in "
                "KosherOS Admin.") from e
        finally:
            if remounted:
                subprocess.run(["mount", "-o", "remount,ro", str(boot)],
                               capture_output=True)
        log.info("boot menu password set")

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
