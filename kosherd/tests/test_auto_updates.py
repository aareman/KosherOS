"""Automatic updates (#19): downloaded in the background on the
administrator's schedule, used at the next restart, never a restart on
their own; one notice per version and a mark in the panel until then.

bootc is never run here: the daemon's subprocess calls and the download
thread are replaced, and what is checked is what the daemon decides from
their answers.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kosherd import access, policy as policy_mod, updates, updatenotify
from kosherd.daemon import Daemon, PolicyError
from kosherd.policy import Policy

ROOT = Path(__file__).resolve().parents[2]
DAY = 86400
NOW = 1_800_000_000.0
EDGE = "ghcr.io/aareman/kosher-linux:edge"


# ---- the setting ------------------------------------------------------------

def test_the_default_is_weekly_with_the_notice_on():
    p = Policy()
    assert (p.auto_updates, p.update_notify) == ("weekly", True)
    doc = p.to_dict()
    assert doc["updates"] == {"auto": "weekly", "notify": True}


def test_an_older_policy_without_the_setting_reads_as_the_default():
    doc = Policy(revision=1).to_dict()
    del doc["updates"]
    p = Policy.from_dict(doc)
    assert (p.auto_updates, p.update_notify) == ("weekly", True)


@pytest.mark.parametrize("schedule", policy_mod.AUTO_UPDATE_SCHEDULES)
def test_every_schedule_survives_a_save_and_a_load(tmp_path, schedule):
    p = Policy(revision=1, auto_updates=schedule, update_notify=False)
    policy_mod.save(p, tmp_path / "policy.json")
    back = policy_mod.load(tmp_path / "policy.json")
    assert (back.auto_updates, back.update_notify) == (schedule, False)


def test_a_schedule_the_schema_does_not_know_is_refused():
    doc = Policy(revision=1).to_dict()
    doc["updates"]["auto"] = "hourly"
    with pytest.raises(PolicyError):
        Policy.from_dict(doc)


def test_both_copies_of_the_schema_say_the_same():
    a = (ROOT / "policy/schema/policy.schema.json").read_text()
    b = (ROOT / "kosherd/src/kosherd/data/policy.schema.json").read_text()
    assert a == b
    assert json.loads(a)["properties"]["updates"]["properties"]["auto"]["enum"] == \
        list(policy_mod.AUTO_UPDATE_SCHEDULES)


# ---- the schedule -----------------------------------------------------------

@pytest.mark.parametrize("schedule,days", [("daily", 1), ("weekly", 7),
                                           ("biweekly", 14), ("monthly", 30)])
def test_each_schedule_is_due_its_period_after_the_last_check_that_worked(schedule, days):
    last = NOW
    due = updates.next_due(schedule, last)
    assert last + days * DAY - updates.DUE_SLACK == due
    # The daily tick wanders a little; a few hours early still counts.
    assert updates.skip_reason(schedule, {"last_success": last},
                               last + days * DAY - 3 * 3600) is None
    assert updates.skip_reason(schedule, {"last_success": last},
                               last + (days - 0.5) * DAY) == "not due yet"


def test_never_checked_is_due_at_once_and_off_is_never_due():
    assert updates.next_due("weekly", None) == 0
    assert updates.skip_reason("weekly", {}, NOW) is None
    assert updates.next_due("off", NOW) is None
    assert updates.skip_reason("off", {}, NOW) == "automatic updates are off"
    # "check now" skips only the wait, never "off".
    assert updates.skip_reason("off", {}, NOW, force=True) == "automatic updates are off"
    assert updates.skip_reason("monthly", {"last_success": NOW}, NOW, force=True) is None


def test_a_metered_network_or_a_low_battery_waits():
    assert updates.is_metered(1) and updates.is_metered(3)
    assert not any(updates.is_metered(v) for v in (0, 2, 4, None))
    assert updates.skip_reason("daily", {}, NOW, metered=True) == "the network is metered"
    assert "battery" in updates.skip_reason("daily", {}, NOW, on_battery=True, battery=20)
    assert updates.skip_reason("daily", {}, NOW, on_battery=True, battery=80) is None
    assert updates.skip_reason("daily", {}, NOW, on_battery=False, battery=5) is None
    assert updates.skip_reason("daily", {}, NOW, busy=True) == "an update is already downloading"


def test_the_timer_ticks_daily_catches_up_and_is_enabled_in_the_image():
    timer = (ROOT / "os-image/files/usr/lib/systemd/system/kosher-auto-update.timer").read_text()
    assert "OnCalendar=daily" in timer and "Persistent=true" in timer
    assert "RandomizedDelaySec=" in timer
    service = (ROOT / "os-image/files/usr/lib/systemd/system/kosher-auto-update.service").read_text()
    assert "ExecStart=/usr/bin/kosherctl updates run --quiet" in service
    assert "reboot" not in service.lower()
    containerfile = (ROOT / "os-image/Containerfile").read_text()
    assert "systemctl enable kosher-auto-update.timer" in containerfile
    assert "systemctl --global enable kosher-update-notify.service" in containerfile
    assert "test -x /usr/bin/kosher-update-notify" in containerfile
    # Fedora's own timer restarts the machine; it stays off.
    assert "systemctl disable bootc-fetch-apply-updates.timer" in containerfile


# ---- the gates --------------------------------------------------------------

def test_changing_the_schedule_is_an_update_right_and_is_logged():
    assert access.ACTIONS["SetAutoUpdates"] == access.ACTION_APPLY_UPDATES
    assert access.ACTIONS["RunAutoUpdate"] == access.ACTION_APPLY_UPDATES
    assert "SetAutoUpdates" in access.CHANGES
    assert "SetAutoUpdates" not in access.GUARDIAN_GATED


def test_any_account_may_ask_whether_an_update_waits_without_a_password():
    assert access.ACTIONS["PendingUpdate"] in access.FREE_ACTIONS
    assert not access.needs_admin_session("PendingUpdate")


def test_the_run_is_answered_off_the_main_loop():
    from kosherd import daemon as daemon_mod

    assert "RunAutoUpdate" in daemon_mod.IN_BACKGROUND


# ---- the daemon, with bootc replaced ----------------------------------------

def _entry(digest, version, key):
    checksum, serial = key.rsplit(".", 1)
    return {"image": {"image": {"image": EDGE, "signature": "containerPolicy"},
                      "version": version, "imageDigest": digest},
            "ostree": {"stateroot": "default", "checksum": checksum,
                       "deploySerial": int(serial)}}


BOOTED = _entry("sha256:old", "0.20.0", "a" * 64 + ".0")
STAGED = _entry("sha256:new", "0.21.0", "b" * 64 + ".0")


class Bootc:
    """Answers `bootc status --json` and `bootc upgrade --check`, and
    records every command."""

    def __init__(self, check="", check_rc=0, check_err="", staged=None):
        self.seen: list = []
        self.check, self.check_rc, self.check_err = check, check_rc, check_err
        self.staged = staged

    def __call__(self, argv, **kw):
        self.seen.append(list(argv))
        out, err, rc = "", "", 0
        if argv[0] == "systemctl" and argv[1] == "is-active":
            out = "inactive\n"
        elif argv[:2] == ["bootc", "status"]:
            out = json.dumps({"status": {"booted": BOOTED, "staged": self.staged,
                                         "rollback": None, "rollbackQueued": False}})
        elif argv[:3] == ["bootc", "upgrade", "--check"]:
            out, err, rc = self.check, self.check_err, self.check_rc
        return type("R", (), {"stdout": out, "stderr": err, "returncode": rc})()


@pytest.fixture
def daemon(monkeypatch, tmp_path):
    state = tmp_path / "auto-update.json"
    monkeypatch.setattr(updates, "AUTO_STATE_PATH", str(state))
    boot = tmp_path / "boot_id"
    boot.write_text("boot-1\n")
    monkeypatch.setattr(updates, "BOOT_ID_PATH", str(boot))
    mark = tmp_path / "staged-deployment"
    mark.write_text("")
    monkeypatch.setattr(updates, "STAGED_MARK", str(mark))
    monkeypatch.setattr(updates, "inspect_registry", lambda image: None)
    d = Daemon.__new__(Daemon)
    d.policy = Policy(revision=1, auto_updates="weekly")
    d.connection = None
    d.signals = []
    d._emit_system_signal = lambda name, sig, args: d.signals.append((name, args))
    d.state = lambda: updates.load_state()
    d.boot = boot
    d.mark = mark
    return d


def _bootc(monkeypatch, **kw) -> Bootc:
    fake = Bootc(**kw)
    monkeypatch.setattr("kosherd.daemon.subprocess.run", fake)
    return fake


AVAILABLE = "Update available for: " + EDGE + "\n  Version: 0.21.0\n  Digest: sha256:new\n"


def test_a_newer_version_is_downloaded_with_the_same_command_as_the_button(daemon, monkeypatch):
    bootc = _bootc(monkeypatch, check=AVAILABLE)
    started = []
    monkeypatch.setattr(updates, "run_upgrade",
                        lambda progress, finished, argv: started.append((argv, finished))
                        or type("T", (), {"is_alive": lambda self: False})())
    result = daemon._auto_update(NOW)
    assert result == {"result": "downloading", "version": "0.21.0"}
    assert started[0][0] == ("bootc", "upgrade")
    assert not [a for a in bootc.seen if "reboot" in a or a[:2] == ["systemctl", "reboot"]]

    # It finishes: staged, noted, and the sessions told once.
    bootc.staged = STAGED
    started[0][1](True, "")
    state = daemon.state()
    assert state["last_success"] == NOW and "last_error" not in state
    assert state["staged_digest"] == "sha256:new"
    assert ("UpdateReady", ("0.21.0", "sha256:new", True)) in daemon.signals
    assert json.loads(daemon.impl_PendingUpdate().unpack()[0]) == \
        {"version": "0.21.0", "digest": "sha256:new"}


def test_the_first_verified_update_uses_the_switch_that_turns_verification_on(
        daemon, monkeypatch):
    _bootc(monkeypatch, check=AVAILABLE)
    monkeypatch.setattr(updates, "upgrade_argv",
                        lambda image, verified: ("bootc", "switch",
                                                 "--enforce-container-sigpolicy", image))
    started = []
    monkeypatch.setattr(updates, "run_upgrade",
                        lambda progress, finished, argv: started.append(argv)
                        or type("T", (), {"is_alive": lambda self: False})())
    daemon._auto_update(NOW)
    assert started == [("bootc", "switch", "--enforce-container-sigpolicy", EDGE)]


def test_up_to_date_is_noted_and_nothing_is_downloaded(daemon, monkeypatch):
    _bootc(monkeypatch, check="No changes in: " + EDGE)
    monkeypatch.setattr(updates, "run_upgrade", lambda *a, **k: pytest.fail("downloaded"))
    assert daemon._auto_update(NOW)["result"] == "none"
    assert daemon.state()["last_success"] == NOW
    # So the next run, a day later, is not due on a weekly schedule.
    assert daemon._auto_update(NOW + DAY)["reason"] == "not due yet"


def test_offline_is_a_failure_tried_again_never_up_to_date(daemon, monkeypatch):
    _bootc(monkeypatch, check_rc=1,
           check_err="error: Upgrading: Fetching: Get manifest: dial tcp: lookup ghcr.io: "
                     "no such host\n")
    monkeypatch.setattr(updates, "run_upgrade", lambda *a, **k: pytest.fail("downloaded"))
    result = daemon._auto_update(NOW)
    assert result["result"] == "failed" and "no such host" in result["error"]
    state = daemon.state()
    assert "last_success" not in state and state["last_attempt"] == NOW
    assert "no such host" in state["last_error"]
    # Due again at the very next tick.
    assert updates.skip_reason("weekly", state, NOW + DAY) is None
    assert not [s for s in daemon.signals if s[0] == "UpdateReady"]


def test_the_run_judges_a_check_as_the_check_button_does(daemon, monkeypatch):
    # One definition of "couldn't check" (updates.judge_check, #207): bootc
    # failed but the registry answered with a newer digest, so it is real.
    _bootc(monkeypatch, check_rc=1, check_err="error: something transient\n")
    monkeypatch.setattr(updates, "inspect_registry",
                        lambda image: {"digest": "sha256:new", "version": "0.21.0"})
    started = []
    monkeypatch.setattr(updates, "run_upgrade",
                        lambda progress, finished, argv: started.append(argv)
                        or type("T", (), {"is_alive": lambda self: False})())
    assert daemon._auto_update(NOW)["result"] == "downloading"
    assert started == [("bootc", "upgrade")]


def test_a_signature_failure_leaves_nothing_staged_and_is_tried_again(daemon, monkeypatch):
    bootc = _bootc(monkeypatch, check=AVAILABLE)
    started = []
    monkeypatch.setattr(updates, "run_upgrade",
                        lambda progress, finished, argv: started.append(finished)
                        or type("T", (), {"is_alive": lambda self: False})())
    undeployed = []
    monkeypatch.setattr(daemon, "_undeploy", lambda keys: undeployed.extend(keys),
                        raising=False)
    daemon._auto_update(NOW)
    # Suppose it staged something before failing: it is taken away.
    bootc.staged = STAGED
    started[0](False, "bootc upgrade failed: Image signature verification failed")
    assert undeployed == ["b" * 64 + ".0"]
    state = daemon.state()
    assert "signature" in state["last_error"] and "last_success" not in state
    assert ("UpdateFinished", (False, "bootc upgrade failed: Image signature "
                                      "verification failed")) in daemon.signals
    assert not [s for s in daemon.signals if s[0] == "UpdateReady"]


def test_a_failed_download_keeps_an_older_staged_update(daemon, monkeypatch):
    before = updates.deployments({"booted": BOOTED, "staged": STAGED})
    assert updates.staged_to_discard(before, before) is None
    assert updates.staged_to_discard(updates.deployments({"booted": BOOTED}),
                                     before) == "b" * 64 + ".0"
    assert updates.staged_to_discard(before, updates.deployments({"booted": BOOTED})) is None


def test_the_version_this_computer_went_back_from_is_not_fetched_again(daemon, monkeypatch):
    assert updates.after_check({"available": True, "on_disk": "rollback"}) == "left"
    assert updates.after_check({"available": True, "on_disk": "staged"}) == "staged"
    assert updates.after_check({"available": True, "on_disk": None}) == "download"
    assert updates.after_check({"available": False}) == "none"


def test_off_does_not_check_at_all(daemon, monkeypatch):
    bootc = _bootc(monkeypatch, check=AVAILABLE)
    daemon.policy.auto_updates = "off"
    assert daemon._auto_update(NOW, force=True)["reason"] == "automatic updates are off"
    assert not [a for a in bootc.seen if a[0] == "bootc"]


def test_while_a_test_build_is_tried_nothing_is_fetched(daemon, monkeypatch):
    _bootc(monkeypatch, check=AVAILABLE)
    monkeypatch.setattr(daemon, "_deployment",
                        lambda: {"booted": {"image": EDGE}, "kept": {"image": EDGE}},
                        raising=False)
    assert daemon._auto_update(NOW)["reason"] == "a test build is being tried"


# ---- telling people, once ---------------------------------------------------

def test_the_same_staged_version_is_announced_once(daemon, monkeypatch):
    _bootc(monkeypatch, staged=STAGED)
    daemon._sync_pending(announce=True)
    daemon._sync_pending(announce=True)
    ready = [s for s in daemon.signals if s[0] == "UpdateReady"]
    assert [args[2] for _n, args in ready] == [True, False]


def test_with_notices_off_the_mark_shows_but_there_is_no_pop_up(daemon, monkeypatch):
    _bootc(monkeypatch, staged=STAGED)
    daemon.policy.update_notify = False
    daemon._sync_pending(announce=True)
    assert daemon.signals == [("UpdateReady", ("0.21.0", "sha256:new", False))]


def test_a_download_by_hand_shows_the_mark_without_a_pop_up(daemon, monkeypatch):
    _bootc(monkeypatch, staged=STAGED)
    daemon._on_update_finished(True, "")
    assert ("UpdateReady", ("0.21.0", "sha256:new", False)) in daemon.signals


def test_a_tried_build_gets_the_pop_up_even_with_notices_off(daemon, monkeypatch):
    # "does try notify once download is done with a restart now?": the
    # person who typed `kosherctl system try` is waiting for it.
    _bootc(monkeypatch, staged=STAGED)
    daemon.policy.update_notify = False
    daemon._trying = True
    daemon._on_update_finished(True, "")
    assert ("UpdateReady", ("0.21.0", "sha256:new", True)) in daemon.signals
    # Once: the next download by hand is the ordinary kind again.
    assert daemon._trying is False


def test_a_tried_build_that_failed_says_nothing_and_forgets_the_try(daemon, monkeypatch):
    _bootc(monkeypatch, staged=STAGED)
    daemon._trying = True
    daemon._on_update_finished(False, "pull failed")
    assert not [s for s in daemon.signals if s[0] == "UpdateReady"]
    assert daemon._trying is False


def test_after_a_restart_or_a_discard_nothing_waits(daemon, monkeypatch):
    bootc = _bootc(monkeypatch, staged=STAGED)
    daemon._sync_pending()
    assert json.loads(daemon.impl_PendingUpdate().unpack()[0])
    # A restart: a new boot id, and the note no longer counts.
    daemon.boot.write_text("boot-2\n")
    assert json.loads(daemon.impl_PendingUpdate().unpack()[0]) == {}
    # A discard (a rollback took it away): the sessions are told.
    daemon.boot.write_text("boot-1\n")
    bootc.staged = None
    daemon._sync_pending()
    assert daemon.signals[-1] == ("UpdateReady", ("", "", False))
    assert json.loads(daemon.impl_PendingUpdate().unpack()[0]) == {}


# ---- the setting, changed ---------------------------------------------------

def test_setting_the_schedule_starts_or_stops_the_timer(daemon, monkeypatch, tmp_path):
    bootc = _bootc(monkeypatch)
    monkeypatch.setattr(policy_mod, "POLICY_PATH", tmp_path / "policy.json")
    monkeypatch.setattr(policy_mod, "save", lambda p: None)
    daemon.impl_SetAutoUpdates("off", True)
    assert ["systemctl", "disable", "--now", "kosher-auto-update.timer"] in bootc.seen
    daemon.impl_SetAutoUpdates("biweekly", False)
    assert ["systemctl", "enable", "--now", "kosher-auto-update.timer"] in bootc.seen
    assert (daemon.policy.auto_updates, daemon.policy.update_notify) == ("biweekly", False)
    with pytest.raises(PolicyError, match="unknown schedule"):
        daemon.impl_SetAutoUpdates("hourly", True)


def test_a_save_that_fails_puts_the_timer_back(daemon, monkeypatch):
    bootc = _bootc(monkeypatch)

    def refuse(_p):
        raise PolicyError("disk full")

    monkeypatch.setattr(policy_mod, "save", refuse)
    with pytest.raises(PolicyError, match="disk full"):
        daemon.impl_SetAutoUpdates("off", True)
    timer = [a for a in bootc.seen if a[0] == "systemctl" and a[-1] == updates.AUTO_TIMER]
    assert [a[1] for a in timer] == ["disable", "enable"]


# ---- the session helper -----------------------------------------------------

class Session:
    def __init__(self):
        self.sent, self.marks, self.reboots = [], [], 0

    def notify(self, summary, body, actions):
        self.sent.append((summary, body, actions))
        return len(self.sent)

    def indicator(self, on):
        self.marks.append(on)

    def reboot(self):
        self.reboots += 1


def _notice(tmp_path):
    s = Session()
    return s, updatenotify.UpdateNotice(s.notify, s.indicator, s.reboot,
                                        tmp_path / "told")


def test_one_pop_up_per_version_even_across_sessions(tmp_path):
    s, notice = _notice(tmp_path)
    notice.ready("0.21.0", "sha256:new", True)
    notice.ready("0.21.0", "sha256:new", True)
    assert [m[0] for m in s.sent] == ["An update is ready"]
    assert s.sent[0][1] == "It'll be used the next time this computer restarts."
    # A new session (or a restarted helper) for the same person.
    s2, again = _notice(tmp_path)
    again.ready("0.21.0", "sha256:new", True)
    assert s2.sent == [] and s2.marks == [True]


def test_the_mark_shows_while_an_update_waits_and_goes_after(tmp_path):
    s, notice = _notice(tmp_path)
    notice.pending_answer({})
    assert s.marks == []
    notice.pending_answer({"version": "0.21.0", "digest": "sha256:new"})
    assert s.marks == [True] and s.sent == [], "no pop-up for an update found at sign-in"
    notice.ready("", "", False)
    assert s.marks == [True, False]


def test_clicking_the_mark_explains_and_restart_asks_kosherd(tmp_path):
    s, notice = _notice(tmp_path)
    notice.ready("0.21.0", "sha256:new", False)
    notice.activate()
    summary, body, actions = s.sent[0]
    assert "Restart the computer" in body
    assert actions == ["restart", "Restart now"]
    notice.action(99, "restart")      # somebody else's notification
    assert s.reboots == 0
    notice.action(1, "restart")
    assert s.reboots == 1


def test_the_mark_is_a_plain_status_notifier_item():
    props = updatenotify.sni_properties()
    assert props["Title"][1] == "Update ready — restart to use it"
    assert props["Status"][1] == "Active"
    assert props["ItemIsMenu"] == ("b", False)
    names = {p for p in props}
    import xml.etree.ElementTree as ET

    declared = {p.get("name") for p in ET.fromstring(updatenotify.SNI_XML).iter("property")}
    assert names == declared


def test_the_helper_runs_in_every_graphical_session():
    unit = (ROOT / "os-image/files/usr/lib/systemd/user/kosher-update-notify.service").read_text()
    assert "ExecStart=/usr/bin/kosher-update-notify" in unit
    assert "WantedBy=graphical-session.target" in unit
    pyproject = (ROOT / "kosherd/pyproject.toml").read_text()
    assert 'kosher-update-notify = "kosherd.updatenotify:main"' in pyproject


# ---- kosherctl --------------------------------------------------------------

def test_kosherctl_says_what_a_run_did():
    from kosherd.cli import auto_run_sentence

    assert auto_run_sentence({"result": "none"}) == "up to date"
    assert "will try again" in auto_run_sentence({"result": "failed", "error": "offline"})
    assert "next restart" in auto_run_sentence({"result": "downloading", "version": "1"})
    assert auto_run_sentence({"result": "skipped", "reason": "not due yet"}) == \
        "not checking: not due yet"


def test_kosherctl_sets_the_schedule_and_the_notice(monkeypatch, capsys):
    from kosherd import cli

    calls = []

    class Fake:
        def auto_update_status(self):
            return {"schedule": "weekly", "notify": True}

        def set_auto_updates(self, schedule, notify):
            calls.append((schedule, notify))

    monkeypatch.setattr(cli, "_client", lambda: Fake())
    for argv in (["updates", "auto", "biweekly"], ["updates", "notify", "off"],
                 ["updates", "auto", "off"]):
        assert cli.main(argv) == 0
    assert cli.main(["updates", "auto", "hourly"]) == 2
    assert calls == [("biweekly", True), ("weekly", False), ("off", True)]


def test_an_update_discarded_behind_kosherds_back_stops_showing(daemon, monkeypatch):
    _bootc(monkeypatch, staged=STAGED)
    daemon._sync_pending()
    assert json.loads(daemon.impl_PendingUpdate().unpack()[0])
    daemon.mark.unlink()     # ostree no longer has anything staged
    daemon.signals.clear()
    assert json.loads(daemon.impl_PendingUpdate().unpack()[0]) == {}
    assert daemon.signals == [("UpdateReady", ("", "", False))]
    assert "staged_digest" not in daemon.state()
