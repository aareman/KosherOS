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

from . import profiles

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


def _user(uid, name, key, admin=False, **kw):
    p = profiles.get(key)
    u = {"uid": uid, "username": name, "mode": p.mode, "admin": admin, "whitelist": [],
         "rules": [], "apps": [], "blocked_categories": list(p.blocked_categories),
         "media_level": p.media_level, "language_filter": p.language_filter,
         "youtube": dict(p.youtube), "can_install_apps": p.can_install_apps,
         "layout": "classic", "cover_style": "frost"}
    u.update(kw)
    return u


class DemoClient:
    """Every method the real DaemonClient has, answered from memory."""

    def __init__(self):
        child = profiles.get("child")
        self.policy = {
            "revision": 3, "guardian": {"enabled": True}, "adblock": {"enabled": True},
            "custom_profiles": [],
            "users": [
                _user(1000, "avi", "adult", admin=True),
                _user(1001, "miriam", "adult", admin=True),
                _user(1002, "yosef", "child",
                      blocked_categories=sorted({*child.blocked_categories, "sports"}),
                      youtube={"restrict": "strict", "blocked_categories": ["24", "20", "10", "17"]},
                      rules=[{"action": "allow", "pattern": "chabad.org"},
                             {"action": "block", "pattern": "youtube.com/shorts*"}]),
                _user(1003, "rivky", "teen"),
                _user(1004, "shmuli", "young_child",
                      whitelist=sorted(f"site{i}.org" for i in range(22))),
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
        return {k: u.get(k) for k in ("username", "mode", "whitelist", "blocked_categories",
                                      "media_level", "language_filter", "youtube")}

    def apply_profile(self, uid, key, pw=""):
        p = profiles.get(key, self.policy["custom_profiles"])
        u = self._user(uid)
        u.update(mode=p.mode, blocked_categories=list(p.blocked_categories),
                 media_level=p.media_level, language_filter=p.language_filter,
                 youtube=dict(p.youtube))
        if "can_install_apps" in u:
            u["can_install_apps"] = p.can_install_apps
        self._change(uid, "ApplyProfile", [uid, key])

    def list_profiles(self): return profiles.describe(self.policy["custom_profiles"])

    def save_profile(self, uid, label, description="", pw=""):
        preset = profiles.to_dict(profiles.from_user(self._user(uid), label, description))
        self.policy["custom_profiles"] = [p for p in self.policy["custom_profiles"]
                                          if p["key"] != preset["key"]] + [preset]
        self._change(uid, "SaveProfile", [uid, label])

    def delete_profile(self, key, pw=""):
        self.policy["custom_profiles"] = [p for p in self.policy["custom_profiles"]
                                          if p["key"] != key]
        self._change(None, "DeleteProfile", [key])

    def list_categories(self):
        from kosherd.categories import CATEGORY_LABELS

        return {"domains": 5_273_843, "version": "2026-09-10", "source": "demo",
                "categories": [{"name": n, "label": l, "present": True}
                               for n, l in sorted(CATEGORY_LABELS.items(),
                                                  key=lambda kv: kv[1].lower())]}

    def create_user(self, username, full_name, mode):
        uid = max(u["uid"] for u in self.policy["users"]) + 1
        key = mode if mode in {p.key for p in profiles.PROFILES} else "child"
        self.policy["users"].append(_user(uid, username, key) if mode in
                                    {p.key for p in profiles.PROFILES}
                                    else {**_user(uid, username, key), "mode": mode})
        self._change(None, "CreateUser", [username])
        return uid

    def adopt_user(self, username, mode):
        return self.create_user(username, username, mode)

    def remove_user(self, uid):
        self.policy["users"] = [u for u in self.policy["users"] if u["uid"] != uid]
        self._change(None, "RemoveUser", [uid])

    def set_guest_config(self, enabled, mode, whitelist, pw=""):
        g = self.policy["guest"]
        g["enabled"] = enabled
        if enabled:
            g.setdefault("uid", 1010)
        if mode in {p.key for p in profiles.PROFILES}:
            p = profiles.get(mode)
            g.update(mode=p.mode, blocked_categories=list(p.blocked_categories),
                     media_level=p.media_level, language_filter=p.language_filter,
                     youtube=dict(p.youtube))
        else:
            g["mode"] = mode
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

    def check_update(self): return "Up to date"
    def apply_update(self): self._change(None, "ApplyUpdate", [])

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
