"""Independent schedules, correct account profiles, and quiet app notices."""

import json
import threading
from concurrent.futures import Future
from pathlib import Path
from types import SimpleNamespace

import pytest

from kosherd import access, apps, appupdates, policy as policy_mod, updates
from kosherd.daemon import Daemon, PolicyError
from kosherd.policy import Policy, UserPolicy
from kosherd.updatenotify import AppUpdateNotice

NOW = 1_800_000_000
APP = {"ref": "org.example.Reader", "latest": "new", "commit": "old"}
NIX = {"ref": "nixpkgs#editor", "latest": "2", "commit": "1"}


def unpack(value):
    return json.loads(value.unpack()[0])


@pytest.fixture
def daemon(monkeypatch, tmp_path):
    monkeypatch.setattr(appupdates, "STATE_PATH", str(tmp_path / "apps.json"))
    d = Daemon.__new__(Daemon)
    d.connection = None
    d.policy = Policy(revision=1, auto_updates="off", app_update_schedule="daily",
                      users=[UserPolicy(uid=1001, username="one", mode="filtered"),
                             UserPolicy(uid=1002, username="two", mode="filtered", developer_tools=True)])
    d._auto_apps_lock = threading.Lock()
    d._power = lambda: (False, 100)
    d._metered = lambda: 0
    d.updated = []
    d.errors = set()

    def update(ref, **kw):
        d.updated.append((ref, kw))
        future = Future()
        if ref in d.errors:
            future.set_exception(apps.AppError("download interrupted"))
        else:
            future.set_result(None)
        return future

    d.app_manager = SimpleNamespace(pending=set(), update=update)
    d._check_nix_updates = lambda uid: [dict(NIX)]
    d._forget_nix_update = lambda uid, ref: None
    monkeypatch.setattr(apps, "refresh_updates", lambda **kw: [dict(APP)])
    return d


def run(d):
    assert d._auto_apps_lock.acquire(blocking=False)
    d._auto_app_updates(appupdates.load_state(), NOW,
                        (d.policy.app_update_schedule, d.policy.app_update_mode),
                        [u.uid for u in d.policy.users])
    assert not d._auto_apps_lock.locked()
    return appupdates.load_state()


def test_old_policy_defaults_apps_off_and_schedules_roundtrip_independently(tmp_path):
    doc = Policy(revision=1).to_dict()
    doc["updates"].pop("apps")
    assert Policy.from_dict(doc).app_update_schedule == "off"
    for system in policy_mod.AUTO_UPDATE_SCHEDULES:
        for app in policy_mod.AUTO_UPDATE_SCHEDULES:
            p = Policy(revision=1, auto_updates=system, app_update_schedule=app,
                       app_update_mode="notify")
            policy_mod.save(p, tmp_path / "policy.json")
            back = policy_mod.load(tmp_path / "policy.json")
            assert (back.auto_updates, back.app_update_schedule, back.app_update_mode) == (
                system, app, "notify")


@pytest.mark.parametrize("key,value", [("schedule", "hourly"), ("mode", "restart")])
def test_invalid_app_settings_are_rejected(key, value):
    doc = Policy(revision=1).to_dict()
    doc["updates"]["apps"][key] = value
    with pytest.raises(PolicyError):
        Policy.from_dict(doc)


def test_settings_control_only_the_app_timer_and_restore_on_failed_save(daemon, monkeypatch):
    timers = []
    daemon._apply_app_update_timer = timers.append
    monkeypatch.setattr(policy_mod, "save", lambda p: None)
    daemon.impl_SetAutoAppUpdates("monthly", "notify")
    assert timers == ["monthly"]
    assert daemon.policy.auto_updates == "off"
    assert daemon.policy.update_notify

    def fail(p):
        raise OSError("disk full")

    monkeypatch.setattr(policy_mod, "save", fail)
    with pytest.raises(OSError):
        daemon.impl_SetAutoAppUpdates("off", "update")
    assert timers == ["monthly", "off", "monthly"]
    assert (daemon.policy.app_update_schedule, daemon.policy.app_update_mode) == ("monthly", "notify")


def test_app_timer_has_its_own_command_and_settings_have_admin_authorization():
    root = Path(__file__).parents[2]
    service = (root / "os-image/files/usr/lib/systemd/system/kosher-auto-app-update.service").read_text()
    timer = (root / "os-image/files/usr/lib/systemd/system/kosher-auto-app-update.timer").read_text()
    assert "updates run --apps --quiet" in service
    assert "OnCalendar=daily" in timer and "Persistent=true" in timer
    assert access.ACTIONS["SetAutoAppUpdates"] == access.ACTION_APPLY_UPDATES
    assert access.ACTIONS["RunAutoAppUpdate"] == access.ACTION_APPLY_UPDATES
    assert "SetAutoAppUpdates" in access.CHANGES
    assert "PendingAppUpdates" in access.UID_AWARE
    assert access.ACTIONS["PendingAppUpdates"] in access.FREE_ACTIONS


def test_apps_update_with_system_updates_off_and_each_nix_profile_is_updated(daemon):
    state = run(daemon)
    assert state["last_success"] == NOW and "last_error" not in state
    assert daemon.updated == [(APP["ref"], {}),
                              (NIX["ref"], {"uid": 1001, "as_root": True}),
                              (NIX["ref"], {"uid": 1002, "as_root": False})]
    assert state["flatpak"] == []
    assert state["accounts"] == {"1001": [], "1002": []}


def test_notify_mode_checks_without_installing_and_does_not_expose_other_accounts(daemon):
    daemon.policy.app_update_mode = "notify"
    daemon._check_nix_updates = lambda uid: [dict(NIX)] if uid == 1001 else []
    state = run(daemon)
    assert not daemon.updated
    assert state["last_success"] == NOW
    assert unpack(daemon.impl_PendingAppUpdates(_uid=1001))["count"] == 2
    other = unpack(daemon.impl_PendingAppUpdates(_uid=1002))
    assert other["count"] == 1 and "editor" not in str(other)
    daemon.policy.app_update_schedule = "off"
    assert unpack(daemon.impl_PendingAppUpdates(_uid=1001)) == {}


def test_failed_install_is_not_success_and_other_profiles_still_update(daemon):
    daemon.errors.add(APP["ref"])
    state = run(daemon)
    assert "last_success" not in state
    assert "download interrupted" in state["last_error"]
    assert state["flatpak"] == [APP]
    assert len(daemon.updated) == 3
    assert updates.next_due("daily", state.get("last_success")) == 0


def test_failed_check_is_recorded_and_other_sources_can_still_update(daemon, monkeypatch):
    def fail(**kw):
        assert kw == {"strict": True}
        raise apps.AppError("network unavailable")
    monkeypatch.setattr(apps, "refresh_updates", fail)
    state = run(daemon)
    assert "network unavailable" in state["last_error"]
    assert "last_success" not in state
    assert len(daemon.updated) == 2


@pytest.mark.parametrize("condition", ["off", "not-due", "metered", "battery", "busy"])
def test_automatic_run_respects_its_own_schedule_network_power_and_busy_queue(daemon, condition):
    if condition == "off":
        daemon.policy.app_update_schedule = "off"
    elif condition == "not-due":
        import time
        appupdates.save_state({"last_success": time.time()})
    elif condition == "metered":
        daemon._metered = lambda: 1
    elif condition == "battery":
        daemon._power = lambda: (True, 10)
    else:
        daemon.app_manager.pending.add("org.example.Running")
    assert unpack(daemon.impl_RunAutoAppUpdate())["result"] == "skipped"
    assert not daemon._auto_apps_lock.locked()
    assert not daemon.updated


def test_settings_changed_during_check_prevent_queued_updates(daemon, monkeypatch):
    def change(**kw):
        daemon.policy.app_update_schedule = "off"
        return [APP]
    monkeypatch.setattr(apps, "refresh_updates", change)
    state = run(daemon)
    assert not daemon.updated
    assert "last_success" not in state
    assert "changed" in state["last_error"]


def test_one_run_at_a_time_returns_promptly(daemon, monkeypatch):
    jobs = []
    class Thread:
        def __init__(self, **kw):
            jobs.append(kw)
        def start(self):
            pass
    monkeypatch.setattr("kosherd.daemon.threading.Thread", Thread)
    assert unpack(daemon.impl_RunAutoAppUpdate())["result"] == "started"
    assert unpack(daemon.impl_RunAutoAppUpdate())["result"] == "skipped"
    assert len(jobs) == 1 and not daemon.updated
    jobs[0]["target"](*jobs[0]["args"])
    assert not daemon._auto_apps_lock.locked()


def test_status_and_notice_do_not_report_an_os_restart(daemon):
    state = run(daemon)
    status = unpack(daemon.impl_AutoAppUpdateStatus())
    assert status["last_success"] == state["last_success"]
    assert status["schedule"] == "daily"
    assert "pending" not in status


def test_app_notices_are_once_per_build_across_sessions_with_no_restart_action(tmp_path):
    sent = []
    path = tmp_path / "notified"
    n = AppUpdateNotice(lambda *args: sent.append(args), path)
    n.pending_answer({"updates": ["a", "b"]})
    n.pending_answer({"updates": ["b"]})
    AppUpdateNotice(lambda *args: sent.append(args), path).pending_answer({"updates": ["a", "b"]})
    assert len(sent) == 1
    assert sent[0][2] == []
    n.pending_answer({"updates": ["a", "b", "c"]})
    assert len(sent) == 2 and "1 app has" in sent[1][1]


def test_failed_notification_is_retried(tmp_path):
    def fail(*args):
        raise RuntimeError("session bus unavailable")
    path = tmp_path / "notified"
    AppUpdateNotice(fail, path).pending_answer({"updates": ["a"]})
    assert not path.exists()


def test_scheduled_flatpak_check_reports_refresh_failure_instead_of_stale_success(monkeypatch):
    def fail(*a):
        raise RuntimeError("offline")
    installation = SimpleNamespace(update_remote_sync=fail, drop_caches=lambda *a: None)
    monkeypatch.setattr(apps, "Flatpak", SimpleNamespace(
        Installation=SimpleNamespace(new_system=lambda _: installation)))
    monkeypatch.setattr(apps, "available_updates", lambda: [])
    with pytest.raises(apps.AppError, match="offline"):
        apps.refresh_updates(strict=True)
    assert apps.refresh_updates() == []  # preserve the Store's existing fallback


def test_app_cli_uses_only_app_settings_and_runner(monkeypatch, capsys):
    from kosherd import cli
    calls = []
    client = SimpleNamespace(
        auto_app_update_status=lambda: {"schedule": "daily", "mode": "notify"},
        set_auto_app_updates=lambda *a: calls.append(a),
        run_auto_app_update=lambda now=False: calls.append(("run", now)) or {"result": "started"})
    monkeypatch.setattr(cli, "_client", lambda: client)
    assert cli.main(["updates", "auto", "weekly", "--apps"]) == 0
    assert calls.pop() == ("weekly", "notify")
    assert cli.main(["updates", "mode", "update", "--apps"]) == 0
    assert calls.pop() == ("daily", "update")
    capsys.readouterr()
    assert cli.main(["updates", "run", "--apps", "--now", "--quiet"]) == 0
    assert calls.pop() == ("run", True)
    assert capsys.readouterr().out == ""


def test_partial_timer_failure_restores_the_previous_schedule(daemon, monkeypatch):
    calls = []
    def apply(schedule):
        calls.append(schedule)
        if schedule == "monthly":
            raise PolicyError("timer start failed")
    daemon._apply_app_update_timer = apply
    saved = []
    monkeypatch.setattr(policy_mod, "save", lambda p: saved.append(p))
    with pytest.raises(PolicyError, match="timer start failed"):
        daemon.impl_SetAutoAppUpdates("monthly", "notify")
    assert calls == ["monthly", "daily"]
    assert not saved
    assert daemon.policy.app_update_schedule == "daily"
    assert daemon.policy.app_update_mode == "update"


def test_account_update_state_is_private_and_atomically_replaced(daemon):
    import stat
    path = Path(appupdates.STATE_PATH)
    appupdates.save_state({"accounts": {"1001": [NIX]}})
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    appupdates.save_state({"accounts": {}})
    assert appupdates.load_state() == {"accounts": {}}
    assert not list(path.parent.glob(".app-updates-*"))
