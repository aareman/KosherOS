"""A pretend kosherd, in memory, for looking at the apps.

`just admin-demo` and `just store-demo` open the real windows against this
instead of D-Bus: no daemon, no polkit, nothing touched. It answers every
method the real DaemonClient has, from a sample family (the one the design
screens were drawn against), and every mutation changes the sample data so
a change made on one screen shows on the others. An app "install" plays
out over a second with progress, the way the real one reports.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from . import profiles, timelimits

now = int(time.time())


def _shipped_catalog() -> list[dict]:
    """The approved apps the image ships (os-image/files/etc/kosher/catalog.json).

    Read from the repository when running from a checkout, from the
    installed copy on a machine, and falling back to one entry if neither
    is there — the demo exists for looking at the UI and must not fail
    because a file moved.
    """
    import json
    from pathlib import Path

    here = Path(__file__).resolve()
    candidates = [Path("/etc/kosher/catalog.json")]
    candidates += [p / "os-image/files/etc/kosher/catalog.json" for p in here.parents]
    for path in candidates:
        try:
            apps = json.loads(path.read_text())["apps"]
        except (OSError, ValueError, KeyError):
            continue
        if apps:
            return apps
    return [{"ref": "org.mozilla.firefox", "name": "Firefox",
             "summary": "Browse the web", "categories": ["Network", "WebBrowser"]}]


# The sample family's groups: made by the family, as they would be. The
# grown-ups are in none; they have the filter on for themselves.
KIDS = profiles.Profile(
    key="custom-kids", label="Kids",
    description="School age: the open web with content filtering.",
    mode="filtered", blocked_categories=profiles.DEFAULTS.blocked_categories,
    media_level="immodest", language_filter="substitute",
    youtube={"restrict": "strict", "blocked_categories": ["24", "20", "10", "shorts"]},
    can_install_apps=False)
TEENS = profiles.Profile(
    key="custom-teens", label="Teens",
    description="More of the web, the same guardrails.",
    mode="filtered",
    blocked_categories=tuple(sorted({*profiles.for_mode("dnsfilter").blocked_categories,
                                     "immodest", "violence", "drugs"})),
    media_level="immodest", language_filter="substitute",
    youtube={"restrict": "moderate"}, can_install_apps=True)
LITTLE_ONES = profiles.Profile(
    key="custom-little-ones", label="Little ones",
    description="Approved sites only, no pictures from the web.",
    mode="whitelist", blocked_categories=profiles.for_mode("whitelist").blocked_categories,
    media_level="all", language_filter="substitute",
    youtube={"restrict": "strict", "allowed_channels": []}, can_install_apps=False)
GROWN_UP = profiles.Profile(
    key="", label="", description="", mode="filtered",
    blocked_categories=tuple(sorted({*profiles.for_mode("dnsfilter").blocked_categories,
                                     "immodest"})),
    media_level="immodest", language_filter="off",
    youtube={"restrict": "moderate"}, can_install_apps=True)
DEMO_GROUPS = (LITTLE_ONES, KIDS, TEENS)


def _settings_for(key):
    """A demo group's settings, a filter mode's defaults, or the grown-ups'."""
    if key == "grown-up":
        return GROWN_UP
    for group in DEMO_GROUPS:
        if group.key == key:
            return group
    return profiles.for_mode(key)


def _user(uid, name, key, admin=False, **kw):
    p = _settings_for(key)
    u = {"uid": uid, "username": name, "mode": p.mode, "admin": admin, "whitelist": [],
         "rules": [], "apps": [], "blocked_categories": list(p.blocked_categories),
         "media_level": p.media_level, "language_filter": p.language_filter,
         "youtube": dict(p.youtube), "can_install_apps": p.can_install_apps,
         "layout": "classic", "cover_style": "frost", "time": {},
         "profile": p.key or None}
    u.update(kw)
    return u


class DemoClient:
    """Every method the real DaemonClient has, answered from memory."""

    def __init__(self):
        self.policy = {
            "revision": 3, "guardian": {"enabled": True}, "adblock": {"enabled": True},
            "custom_profiles": [profiles.to_dict(g) for g in DEMO_GROUPS],
            "users": [
                _user(1000, "avi", "grown-up", admin=True),
                _user(1001, "miriam", "grown-up", admin=True),
                _user(1002, "yosef", "custom-kids",
                      blocked_categories=sorted({*KIDS.blocked_categories, "sports"}),
                      youtube={"restrict": "strict", "blocked_categories": ["24", "20", "10", "17", "shorts"]},
                      rules=[{"action": "allow", "pattern": "chabad.org"},
                             {"action": "block", "pattern": "youtube.com/shorts*"}],
                      time={"daily_minutes": 120,
                            "allowed": timelimits.SCHEDULE_PRESETS["after_school"]}),
                _user(1003, "rivky", "custom-teens",
                      time={"allowed": timelimits.SCHEDULE_PRESETS["not_late"]}),
                _user(1004, "shmuli", "custom-little-ones",
                      whitelist_bundles=["torah"],
                      whitelist=["chinuch.org", "ourfamily.example"],
                      time={"daily_minutes": 60}),
            ],
            "guest": {"enabled": False, "mode": "whitelist", "whitelist": []},
        }
        self.requests = [
            {"id": "a" * 32, "uid": 1002, "username": "yosef", "mode": "filtered",
             "url": "https://www.youtube.com/watch?v=k2Qx9", "note": "for school",
             "asked": now - 1200, "why": "category:video"},
            {"id": "b" * 32, "uid": 1003, "username": "rivky", "mode": "filtered",
             "url": "https://www.pinterest.com/pin/8813", "note": "sewing pattern",
             "asked": now - 7200, "why": "category:social"},
        ]
        self.events = [
            {"t": now - 300, "kind": "block", "uid": 1002, "username": "yosef",
             "url": "https://www.youtube.com/watch?v=9fb", "why": "category:video"},
            {"t": now - 2000, "kind": "block", "uid": 1002, "username": "yosef",
             "url": "https://roblox.com/", "why": "category:games"},
            {"t": now - 2100, "kind": "block", "uid": 1002, "username": "yosef",
             "url": "https://roblox.com/", "why": "category:games"},
            {"t": now - 3000, "kind": "pictures", "uid": 1002, "username": "yosef",
             "url": "https://en.wikipedia.org/wiki/Beach"},
            {"t": now - 9000, "kind": "block", "uid": 1003, "username": "rivky",
             "url": "https://twitch.tv/", "why": "category:video"},
            {"t": now - 12000, "kind": "search", "uid": 1003, "username": "rivky",
             "text": "something rude", "why": "it contains a blocked word"},
            {"t": now - 86400 - 1200, "kind": "time", "uid": 1002, "username": "yosef",
             "why": "time:limit"},
            {"t": now - 86400 - 900, "kind": "time", "uid": 1002, "username": "yosef",
             "why": "time:login"},
            {"t": now - 86400 - 3000, "kind": "change", "uid": 1003, "username": "rivky",
             "by": 1001, "by_username": "miriam", "method": "ApplyProfile",
             "args": [1003, "teen"], "guardian": True},
            {"t": now - 86400 - 9000, "kind": "change", "uid": 1002, "username": "yosef",
             "by": 1000, "by_username": "avi", "method": "SetBlockedCategories",
             "args": [1002, ["adult", "sports"]], "guardian": True},
        ]
        self.installed = [
            {"ref": "org.gimp.GIMP", "icon_name": "gimp", "name": "GIMP", "installed_by": "avi", "approved": True},
            {"ref": "org.libreoffice.LibreOffice", "icon_name": "x-office-document", "name": "LibreOffice", "installed_by": "",
             "approved": True},
            {"ref": "org.mozilla.firefox", "icon_name": "firefox", "name": "Firefox", "installed_by": "avi",
             "approved": True},
        ]
        # The shelf a real machine ships with, read from the image's own
        # catalogue so the demo cannot drift from what a family sees.
        self.catalog = _shipped_catalog()

        self._on_progress = self._on_finished = None
        self._on_time_warning = None
        # Seconds of active use so far today, as the real daemon counts them.
        self.used_today = {1002: 80 * 60, 1003: 35 * 60, 1004: 60 * 60}
        self.status = {"pictures": "no_model", "detect_ms": None, "degraded": [],
                       "problems": [],
                       "services": {"kosher-mitm.service": "active", "kosher-dns.service": "active",
                                    "kosher-search.service": "active",
                                    "kosher-searxng.service": "active"}}

    # -- helpers --------------------------------------------------------------

    def _user(self, uid):
        for u in self.policy["users"]:
            if u["uid"] == uid:
                return u
        g = self.policy["guest"]
        if g.get("enabled") and g.get("uid") == uid:
            return g
        raise ValueError(f"uid {uid} is not managed")

    def _change(self, uid, method, args):
        self.policy["revision"] += 1
        who = self._user(uid) if uid is not None else None
        self.events.insert(0, {"t": int(time.time()), "kind": "change",
                               "uid": uid if uid is not None else -1,
                               "username": (who or {}).get("username", "?"),
                               "by": 1000, "by_username": "avi", "method": method,
                               "args": args, "guardian": self.policy["guardian"]["enabled"]})

    # -- session ---------------------------------------------------------------

    def setup_complete(self): return True
    def admin_exists(self): return True, "avi"
    def existing_accounts(self): return []
    def create_first_admin(self, *a): return 1000
    def finish_setup(self, *a): return None
    def unlock(self): return None
    def lock(self): return None
    def session_status(self): return True, 3600, True
    def verify_guardian(self, password): return None
    def guardian_enabled(self): return self.policy["guardian"]["enabled"]

    def set_guardian_password(self, old, new):
        self.policy["guardian"]["enabled"] = True
        self._change(None, "SetGuardianPassword", [])

    def disable_guardian(self, pw=""):
        self.policy["guardian"]["enabled"] = False
        self._change(None, "DisableGuardian", [])

    # -- policy ----------------------------------------------------------------

    def get_policy(self): return json.loads(json.dumps(self.policy))

    def set_filter_mode(self, uid, mode, pw=""):
        self._user(uid)["mode"] = mode
        self._change(uid, "SetFilterMode", [uid, mode])

    def set_whitelist(self, uid, domains, pw=""):
        self._user(uid)["whitelist"] = sorted(domains)
        self._change(uid, "SetWhitelist", [])

    def list_whitelist_bundles(self):
        """The ready-made approved-site lists, read from the image's own file
        so the demo shows exactly what a machine offers."""
        from . import whitelists

        found = whitelists.describe()
        if found:
            return found
        here = Path(__file__).resolve()
        for parent in here.parents:
            candidate = parent / "os-image/files/usr/share/kosher/whitelist-bundles.json"
            if candidate.exists():
                whitelists.BUNDLE_PATH = candidate
                whitelists._cache = None
                return whitelists.describe()
        return []

    def set_whitelist_bundles(self, uid, bundles, pw=""):
        self._user(uid)["whitelist_bundles"] = sorted(bundles)
        self._change(uid, "SetWhitelistBundles", [uid, sorted(bundles)])

    def set_url_rules(self, uid, rules, pw=""):
        self._user(uid)["rules"] = list(rules)
        self._change(uid, "SetUrlRules", [])

    def set_blocked_categories(self, uid, categories, pw=""):
        self._user(uid)["blocked_categories"] = sorted(categories)
        self._change(uid, "SetBlockedCategories", [uid, sorted(categories)])

    def set_media_level(self, uid, level, pw=""):
        self._user(uid)["media_level"] = level
        self._change(uid, "SetMediaLevel", [uid, level])

    def set_language_filter(self, uid, setting, pw=""):
        self._user(uid)["language_filter"] = setting
        self._change(uid, "SetLanguageFilter", [uid, setting])

    def set_youtube(self, uid, settings, pw=""):
        self._user(uid)["youtube"] = dict(settings)
        self._change(uid, "SetYouTube", [uid])

    def set_user_admin(self, uid, admin, pw=""):
        self._user(uid)["admin"] = admin
        self._change(uid, "SetUserAdmin", [uid, admin])

    def set_layout(self, uid, layout):
        self._user(uid)["layout"] = layout
        self._change(uid, "SetLayout", [uid, layout])

    def set_cover_style(self, uid, style):
        self._user(uid)["cover_style"] = style
        self._change(uid, "SetCoverStyle", [uid, style])

    def set_adblock(self, enabled, pw=""):
        self.policy["adblock"]["enabled"] = enabled
        self._change(None, "SetAdBlock", [enabled])

    def my_layout(self): return "classic"

    def get_my_settings(self):
        """What My Filter shows the person at the keyboard: in the demo, yosef."""
        u = self._user(1002)
        settings = {k: u.get(k) for k in ("username", "mode", "whitelist", "blocked_categories",
                                          "media_level", "language_filter", "youtube")}
        settings["managed"] = True
        state = self._time_state(u)
        settings["time"] = {
            "admin": False, "limited": state["limited"],
            "daily_minutes": timelimits.daily_minutes(u.get("time")),
            "allowed": timelimits.grid(u.get("time")),
            "today": timelimits.grid(u.get("time"))[time.localtime().tm_wday],
            "used": state["used"], "left": state["left"], "block_ends": state["block_ends"],
        }
        return settings

    # -- time ---------------------------------------------------------------------

    def _time_state(self, u):
        return timelimits.status(u.get("time"), self.used_today.get(u["uid"], 0))

    def set_time_limits(self, uid, settings, pw=""):
        self._user(uid)["time"] = timelimits.parse(settings)
        self._change(uid, "SetTimeLimits", [uid, timelimits.parse(settings)])

    def time_usage(self):
        out = {}
        for u in self.policy["users"]:
            if u.get("admin"):
                out[str(u["uid"])] = {"admin": True, "limited": False, "used": 0,
                                      "signed_in": u["uid"] == 1000}
                continue
            state = self._time_state(u)
            state["signed_in"] = u["uid"] in (1002, 1003)
            out[str(u["uid"])] = state
        g = self.policy["guest"]
        if g.get("enabled") and g.get("uid") is not None:
            out[str(g["uid"])] = {**self._time_state(g), "signed_in": False}
        return out

    def connect_time_signals(self, on_warning):
        self._on_time_warning = on_warning
        return 2

    def apply_profile(self, uid, key, pw=""):
        u = self._user(uid)
        if not key:
            u["profile"] = None
            self._change(uid, "ApplyProfile", [uid, key])
            return
        p = profiles.get(key, self.policy["custom_profiles"])
        profiles.apply(u, p)
        u["profile"] = key
        self._change(uid, "ApplyProfile", [uid, key])

    def list_profiles(self): return profiles.describe(self.policy["custom_profiles"])

    def save_profile(self, uid, label, description="", pw=""):
        group = profiles.from_user(self._user(uid), label, description)
        self.policy["custom_profiles"] = [p for p in self.policy["custom_profiles"]
                                          if p["key"] != group.key] + [profiles.to_dict(group)]
        self._user(uid)["profile"] = group.key
        for member in profiles.members(self.policy["users"], group.key):
            profiles.apply(member, group)
        self._change(uid, "SaveProfile", [uid, label])
        return group.key

    def delete_profile(self, key, pw=""):
        self.policy["custom_profiles"] = [p for p in self.policy["custom_profiles"]
                                          if p["key"] != key]
        for member in profiles.members(self.policy["users"], key):
            member["profile"] = None
        self._change(None, "DeleteProfile", [key])

    def list_categories(self):
        from kosherd.categories import CATEGORY_LABELS

        return {"domains": 5_273_843, "version": "2026-09-10", "source": "demo",
                "categories": [{"name": n, "label": l, "present": True}
                               for n, l in sorted(CATEGORY_LABELS.items(),
                                                  key=lambda kv: kv[1].lower())]}

    def create_user(self, username, full_name, mode):
        uid = max(u["uid"] for u in self.policy["users"]) + 1
        groups = {p["key"] for p in self.policy["custom_profiles"]}
        if mode in groups:
            user = _user(uid, username, "filtered")
            profiles.apply(user, profiles.get(mode, self.policy["custom_profiles"]))
            user["profile"] = mode
        else:
            user = _user(uid, username, mode)
        self.policy["users"].append(user)
        self._change(None, "CreateUser", [username])
        return uid

    def adopt_user(self, username, mode):
        return self.create_user(username, username, mode)

    def reset_password(self, uid):
        user = self._user(uid)
        if user.get("admin"):
            raise RuntimeError(f"{user['username']} is an administrator, and an "
                               "administrator changes their own password in Settings")
        user["password_at_login"] = True
        self._change(uid, "ResetPassword", [uid])

    def remove_user(self, uid):
        self.policy["users"] = [u for u in self.policy["users"] if u["uid"] != uid]
        self._change(None, "RemoveUser", [uid])

    def set_guest_config(self, enabled, mode, whitelist, pw=""):
        g = self.policy["guest"]
        g["enabled"] = enabled
        if enabled:
            g.setdefault("uid", 1010)
        groups = {p["key"] for p in self.policy["custom_profiles"]}
        p = (profiles.get(mode, self.policy["custom_profiles"]) if mode in groups
             else profiles.for_mode(mode))
        g.update(mode=p.mode, blocked_categories=list(p.blocked_categories),
                 media_level=p.media_level, language_filter=p.language_filter,
                 youtube=dict(p.youtube))
        g["whitelist"] = sorted(whitelist)
        self._change(None, "SetGuestConfig", [enabled, mode])

    def set_captive_mode(self, uid, minutes): self._change(uid, "SetCaptiveMode", [uid, minutes])

    # -- lists -------------------------------------------------------------------

    def get_list_edits(self, name):
        return {"add": {"blast": "bother"} if name == "wordlist.json" else {},
                "remove": [], "shipped": 119}

    def edit_list(self, name, add, remove, pw=""): self._change(None, "EditList", [name])

    # -- requests and activity -------------------------------------------------

    def filter_status(self): return dict(self.status)
    def list_requests(self): return list(self.requests)

    def approve_request(self, request_id, whole_site=False, pw=""):
        self.requests = [r for r in self.requests if r["id"] != request_id]
        self._change(None, "ApproveRequest", [])

    def dismiss_request(self, request_id):
        self.requests = [r for r in self.requests if r["id"] != request_id]
        self._change(None, "DismissRequest", [])

    def allow_url(self, uid, url, whole_site=False, pw=""):
        self._change(uid, "AllowUrl", [uid, url, whole_site])

    def list_activity(self, since=0, uid=-1):
        return [e for e in self.events if e["t"] >= since and (uid < 0 or e["uid"] == uid)]

    def activity_summary(self):
        from kosherd import activity

        today = activity.day_start()
        return {str(u): c for u, c in activity.summary(
            [e for e in self.events if e["t"] >= today]).items()}

    # -- apps ----------------------------------------------------------------------

    def list_catalog(self): return {"apps": list(self.catalog)}
    def list_installed(self): return [a["ref"] for a in self.installed]
    def list_installed_details(self): return list(self.installed)
    def set_user_apps(self, uid, refs): self._user(uid)["apps"] = list(refs)
    def install_app(self, ref):
        """Pretend to install: progress over a second, then finished."""
        from gi.repository import GLib

        steps = [(20, "Downloading"), (60, "Downloading"), (90, "Installing"), (100, "Done")]

        def tick(i=0):
            if i < len(steps):
                percent, status = steps[i]
                if self._on_progress:
                    self._on_progress(ref, percent, status)
                GLib.timeout_add(250, tick, i + 1)
            else:
                name = next((a["name"] for a in self.catalog if a["ref"] == ref), ref)
                self.installed.append({"ref": ref, "name": name, "installed_by": "you",
                                       "approved": True})
                if self._on_finished:
                    self._on_finished(ref, True, "")
            return False

        GLib.timeout_add(250, tick)

    # -- updates: the first two installed apps have a newer build ------------------

    def check_app_updates(self):
        return self.list_app_updates()

    def list_app_updates(self):
        pending = getattr(self, "_updated", set())
        out = []
        for app in self.installed[:2]:
            if app["ref"] in pending:
                continue
            out.append({"ref": app["ref"], "name": app["name"], "version": "2.1",
                        "commit": "a1b2c3d4e5f6", "latest": "f6e5d4c3b2a1"})
        return out

    def update_app(self, ref):
        """Pretend to update: progress over a second, then finished."""
        from gi.repository import GLib

        steps = [(15, "Downloading"), (55, "Downloading"), (90, "Updating…"), (100, "Done")]

        def tick(i=0):
            if i < len(steps):
                percent, status = steps[i]
                if self._on_progress:
                    self._on_progress(ref, percent, status)
                GLib.timeout_add(250, tick, i + 1)
            else:
                self.__dict__.setdefault("_updated", set()).add(ref)
                if self._on_finished:
                    self._on_finished(ref, True, "")
            return False

        GLib.timeout_add(250, tick)

    def update_all_apps(self):
        refs = [u["ref"] for u in self.list_app_updates()]
        for ref in refs:
            self.update_app(ref)
        return len(refs)

    def remove_app(self, ref):
        from gi.repository import GLib

        self.installed = [a for a in self.installed if a["ref"] != ref]
        if self._on_finished:
            GLib.timeout_add(300, lambda: (self._on_finished(ref, True, ""), False)[1])

    def set_user_can_install(self, uid, can): self._user(uid)["can_install_apps"] = can

    def search_apps(self, query):
        return [{"ref": f"org.example.{query.title()}{i}", "name": f"{query.title()} {i}",
                 "summary": "A search result"} for i in range(1, 6)]

    def approve_app(self, ref, name="", summary=""):
        self.catalog.append({"ref": ref, "name": name or ref, "summary": summary})

    def unapprove_app(self, ref):
        self.catalog = [a for a in self.catalog if a["ref"] != ref]

    def connect_app_signals(self, on_progress, on_finished):
        self._on_progress, self._on_finished = on_progress, on_finished
        return 1

    # -- system --------------------------------------------------------------------

    def filter_log(self, lines=200):
        return ("2026-09-16T10:00:01+0000 kosheros kosher-mitm[812]: blocked a YouTube video (category)\n"
                "2026-09-16T10:00:07+0000 kosheros kosher-mitm[812]: hid a picture from example.com\n")

    def check_update(self):
        return {"ok": True, "available": True, "version": "0.1.0-pre.12", "channel": "edge",
                "image": "ghcr.io/aareman/kosher-linux:edge", "digest": "sha256:demo",
                "raw": "Update available for: ghcr.io/aareman/kosher-linux:edge\n"
                       "  Version: 0.1.0-pre.12"}

    def connect_update_signals(self, on_progress, on_finished):
        self._on_update_progress, self._on_update_finished = on_progress, on_finished
        return 2

    def disconnect_signals(self, subscription):
        if subscription == 2:
            self._on_update_progress = self._on_update_finished = None

    def apply_update(self):
        """Pretend to update: a pull that takes a few seconds, then staged."""
        from gi.repository import GLib

        self._change(None, "ApplyUpdate", [])
        steps = [(0, "Pulling image (1 of 4)"), (18, "Pulling image (1 of 4)"),
                 (41, "Pulling image (2 of 4)"), (67, "Pulling image (3 of 4)"),
                 (88, "Pulling image (4 of 4)"), (96, "Deploying"), (100, "Update ready")]

        def tick(i=0):
            progress = getattr(self, "_on_update_progress", None)
            finished = getattr(self, "_on_update_finished", None)
            if i < len(steps):
                if progress:
                    progress(*steps[i])
                GLib.timeout_add(600, tick, i + 1)
            elif finished:
                finished(True, "")
            return False

        GLib.timeout_add(300, tick)

    # -- first-boot network ------------------------------------------------------

    def network_status(self):
        return {"online": False, "kind": "", "name": "", "wifi_hardware": True}

    def list_wifi(self):
        return [{"ssid": "Home", "signal": 82, "secured": True, "active": False},
                {"ssid": "Shul Guest", "signal": 61, "secured": False, "active": False},
                {"ssid": "Next door", "signal": 34, "secured": True, "active": False}]

    def connect_wifi(self, ssid, password=""):
        if ssid == "Home" and password != "letmein":
            raise RuntimeError("That password was not accepted. Check it and try again.")
        self.network_status = lambda: {"online": True, "kind": "wifi", "name": ssid,
                                       "wifi_hardware": True}

    def reboot(self):
        self._change(None, "Reboot", [])

    def deployment_status(self):
        return {"booted": {"image": "ghcr.io/aareman/kosher-linux:edge", "version": "0.1.0-pre.11",
                           "timestamp": now - 3600},
                "rollback": {"image": "ghcr.io/aareman/kosher-linux:edge",
                             "version": "0.1.0-pre.9", "timestamp": now - 90000},
                "staged": None, "rollback_queued": False}

    def rollback(self): self._change(None, "Rollback", [])

    # -- portal ------------------------------------------------------------------------

    def enrol(self, url, code, pw=""): return "demo-device"
    def unenrol(self, pw=""): return None
    def sync_now(self): return False
    def portal_status(self): return {"enrolled": False}
