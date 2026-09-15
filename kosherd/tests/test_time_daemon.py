"""Time limits at the daemon's door and in the image.

The D-Bus surface itself needs a live bus (see pyproject's coverage note),
so the methods are driven directly on a Daemon built without one, and the
image wiring — PAM, the user unit, the state directory, the entry points —
is read out of the files that ship.
"""

import inspect
import json
from pathlib import Path

import pytest

from kosherd import daemon as daemon_mod
from kosherd import timelimits
from kosherd.daemon import Daemon
from kosherd.policy import GuestPolicy, Policy, PolicyError, UserPolicy

ROOT = Path(__file__).parents[2]
FILES = ROOT / "os-image/files"
CONTAINERFILE = (ROOT / "os-image/Containerfile").read_text()
USER_UNIT = FILES / "usr/lib/systemd/user/kosher-time-notify.service"
TMPFILES = (FILES / "usr/lib/tmpfiles.d/kosher.conf").read_text()
PYPROJECT = (ROOT / "kosherd/pyproject.toml").read_text()


def _daemon(users, guest=None):
    daemon = Daemon.__new__(Daemon)
    daemon.policy = Policy(revision=1, users=users, guest=guest or GuestPolicy())
    daemon.connection = None
    daemon.applied = 0
    daemon._save_and_apply = lambda: setattr(daemon, "applied", daemon.applied + 1)
    return daemon


CHILD = UserPolicy(uid=1001, username="yosef", mode="filtered")
ADMIN = UserPolicy(uid=1000, username="abba", mode="filtered", admin=True)
TWO_HOURS = {"daily_minutes": 120, "allowed": timelimits.SCHEDULE_PRESETS["not_late"]}


# -- the methods -----------------------------------------------------------------

def test_setting_limits_saves_them_in_their_compact_form_and_applies():
    daemon = _daemon([CHILD])
    daemon.impl_SetTimeLimits(1001, json.dumps({**TWO_HOURS, "allowed": [timelimits.ALWAYS] * 7}), "")
    assert daemon.policy.user(1001).time == {"daily_minutes": 120}, "always allowed is left out"
    assert daemon.applied == 1
    daemon.impl_SetTimeLimits(1001, json.dumps({"daily_minutes": 0}), "")
    assert daemon.policy.user(1001).time == {} and daemon.applied == 2


def test_the_guest_can_be_limited_like_anyone():
    daemon = _daemon([CHILD], guest=GuestPolicy(enabled=True, uid=1010))
    daemon.impl_SetTimeLimits(1010, json.dumps(TWO_HOURS), "")
    assert daemon.policy.guest.time == TWO_HOURS
    guest = next(u for u in daemon.policy.effective_users() if u.uid == 1010)
    assert guest.time == TWO_HOURS, "and enforcement sees it"


def test_administrators_are_never_limited():
    daemon = _daemon([ADMIN, CHILD])
    with pytest.raises(PolicyError, match="never limited"):
        daemon.impl_SetTimeLimits(1000, json.dumps(TWO_HOURS), "")
    assert daemon.policy.user(1000).time == {} and daemon.applied == 0


@pytest.mark.parametrize("bad", ["not json", json.dumps({"daily_minutes": -5}),
                                 json.dumps({"allowed": ["1" * 24] * 3}),
                                 json.dumps(["1", "2"])])
def test_malformed_settings_are_refused_before_anything_changes(bad):
    daemon = _daemon([CHILD])
    with pytest.raises(PolicyError):
        daemon.impl_SetTimeLimits(1001, bad, "")
    assert daemon.applied == 0


def test_an_unmanaged_account_is_refused():
    daemon = _daemon([CHILD])
    with pytest.raises(PolicyError):
        daemon.impl_SetTimeLimits(4242, json.dumps(TWO_HOURS), "")


def test_usage_comes_from_the_timekeeper_keyed_by_uid(tmp_path):
    from kosherd.timekeeper import Timekeeper

    daemon = _daemon([ADMIN, UserPolicy(uid=1001, username="yosef", mode="filtered",
                                        time=TWO_HOURS)])
    daemon.timekeeper = Timekeeper(
        lambda: daemon.policy, lambda: [], timelimits.UsageStore(tmp_path / "u.jsonl"),
        warn=lambda *a: None, lock=lambda uid: None, terminate=lambda uid: None,
        write_conf=lambda policy, exhausted: None)
    usage = json.loads(daemon.impl_GetTimeUsage().unpack()[0])
    assert set(usage) == {"1000", "1001"}
    assert usage["1000"]["admin"] is True
    assert usage["1001"]["limit"] == 120 * 60 and usage["1001"]["limited"] is True


def test_the_bus_offers_the_methods_and_the_warning_signal():
    xml = daemon_mod.INTROSPECTION_XML
    assert '<method name="SetTimeLimits">' in xml
    assert '<method name="GetTimeUsage">' in xml
    assert '<signal name="TimeWarning">' in xml
    assert 'name="minutes_left"' in xml


def test_every_policy_change_re_renders_the_sign_in_schedule():
    # The admin app's change must be live at once, in both save paths: the
    # local one and the portal's.
    assert "self._apply_time()" in inspect.getsource(Daemon._save_and_apply)
    assert "self._apply_time()" in inspect.getsource(Daemon.sync_now)
    assert "policy_changed" in inspect.getsource(Daemon._apply_time)


def test_the_first_look_happens_when_the_bus_is_up_and_then_every_minute():
    assert "self._time_tick()" in inspect.getsource(Daemon._on_bus_acquired)
    assert "GLib.timeout_add_seconds(TICK_SECONDS, self._time_tick)" in \
        inspect.getsource(Daemon.run)
    assert "return True" in inspect.getsource(Daemon._time_tick), "the timeout must be kept"


def test_a_tick_that_fails_does_not_stop_the_clock():
    daemon = Daemon.__new__(Daemon)

    class Broken:
        def tick(self):
            raise RuntimeError("logind went away")

    daemon.timekeeper = Broken()
    assert daemon._time_tick() is True


def test_without_a_bus_there_are_no_sessions_and_nothing_to_lock():
    daemon = Daemon.__new__(Daemon)
    daemon.connection = None
    assert daemon._logind_sessions() == []
    daemon._lock_uid(1001)
    daemon._terminate_uid(1001)
    daemon._emit_time_warning(1001, 5, "limit")


def test_greeter_and_lock_screen_sessions_are_not_a_person():
    # The adapter drops every logind class but "user"; check the rule is
    # in the code rather than trust it.
    source = inspect.getsource(Daemon._logind_sessions)
    assert 'props.get("Class", "user") != "user"' in source
    assert "IdleHint" in source and "Active" in source


# -- the image -------------------------------------------------------------------

def test_the_warning_helper_runs_in_every_graphical_session():
    unit = USER_UNIT.read_text()
    assert "ExecStart=/usr/bin/kosher-time-notify" in unit
    assert "WantedBy=graphical-session.target" in unit
    assert "ConditionUser=!@system" in unit
    assert "systemctl --global enable kosher-time-notify.service" in CONTAINERFILE
    assert 'kosher-time-notify = "kosherd.timenotify:main"' in PYPROJECT


def test_pam_time_guards_the_login_screen_and_the_console():
    for service in ("gdm-password", "gdm-autologin", "login"):
        assert service in CONTAINERFILE
    assert "pam_time.so" in CONTAINERFILE
    # A refusal continues to pam_exec (which logs it) and still fails.
    assert "[success=1 default=bad] pam_time.so" in CONTAINERFILE
    assert "pam_exec.so quiet /usr/bin/kosher-time-refused" in CONTAINERFILE
    assert "grep -q pam_time /etc/pam.d/gdm-password" in CONTAINERFILE, \
        "the build must fail if the edit did not land"
    assert 'kosher-time-refused = "kosherd.timelimits:refused_login_main"' in PYPROJECT
    # authselect's own files are left alone.
    assert "/etc/pam.d/password-auth" not in CONTAINERFILE
    assert "/etc/pam.d/system-auth" not in CONTAINERFILE


def test_the_days_budget_has_a_root_only_home_that_outlives_a_reboot():
    assert "d /var/lib/kosher-time 0700 root root -" in TMPFILES
    assert timelimits.USAGE_PATH == Path("/var/lib/kosher-time/usage.jsonl")
    assert str(timelimits.TIME_CONF) == "/etc/security/time.conf"
