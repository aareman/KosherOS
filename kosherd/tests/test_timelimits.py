"""Time limits: the settings, the clock arithmetic, pam_time and the words."""

import json
import time

import pytest

from kosherd import activity, timelimits as tl
from kosherd.policy import Policy, UserPolicy


def local(year, month, day, hour, minute=0):
    """A timestamp for a local wall-clock moment, so weekday and hour are
    what the test says whatever zone the machine is in."""
    return time.mktime((year, month, day, hour, minute, 0, 0, 0, -1))


# 2026-09-14 is a Monday.
MONDAY_10 = local(2026, 9, 14, 10)
SATURDAY_10 = local(2026, 9, 19, 10)


# -- the settings --------------------------------------------------------------

def test_nothing_set_means_no_limit_and_always_allowed():
    assert tl.parse(None) == {}
    assert tl.parse({}) == {}
    assert tl.daily_minutes({}) == 0
    assert tl.grid({}) == [tl.ALWAYS] * 7
    assert not tl.is_limited({})


def test_defaults_are_dropped_so_the_policy_stays_small():
    assert tl.parse({"daily_minutes": 0, "allowed": [tl.ALWAYS] * 7}) == {}
    kept = tl.parse({"daily_minutes": 120, "allowed": tl.SCHEDULE_PRESETS["not_late"]})
    assert kept == {"daily_minutes": 120, "allowed": tl.SCHEDULE_PRESETS["not_late"]}
    assert tl.is_limited(kept)
    assert tl.is_limited({"daily_minutes": 30})
    assert tl.is_limited({"allowed": tl.SCHEDULE_PRESETS["weekdays"]})


@pytest.mark.parametrize("bad", [
    "2 hours", [], {"daily_minutes": -1}, {"daily_minutes": 1441},
    {"daily_minutes": True}, {"daily_minutes": 30.5}, {"hours": 2},
    {"allowed": [tl.ALWAYS] * 6}, {"allowed": ["1" * 23] * 7},
    {"allowed": ["1" * 24] * 6 + ["1" * 23 + "2"]}, {"allowed": "always"},
])
def test_anything_else_is_refused(bad):
    with pytest.raises(tl.TimeError):
        tl.parse(bad)


def test_the_presets_are_well_formed_and_the_limits_include_no_limit():
    for key, grid in tl.SCHEDULE_PRESETS.items():
        assert tl.parse({"allowed": grid}) is not None, key
        assert len(grid) == 7 and all(len(d) == 24 for d in grid)
    assert tl.is_always(tl.SCHEDULE_PRESETS["always"])
    assert 0 in tl.LIMIT_PRESET_MINUTES and 120 in tl.LIMIT_PRESET_MINUTES
    after = tl.SCHEDULE_PRESETS["after_school"]
    assert tl.hours_text(after[0]) == "15:00–20:00"     # Monday
    assert tl.hours_text(after[5]) == "09:00–20:00"     # Saturday
    assert tl.hours_text(tl.SCHEDULE_PRESETS["weekdays"][6]) == "Not at all"


def test_the_policy_carries_time_for_users_and_the_guest():
    doc = Policy(revision=1, users=[UserPolicy(
        uid=1001, username="yosef", mode="filtered",
        time={"daily_minutes": 120})]).to_dict()
    doc["guest"] = {"enabled": True, "uid": 1010, "time": {"allowed": tl.SCHEDULE_PRESETS["not_late"]}}
    policy = Policy.from_dict(doc)
    assert policy.user(1001).time == {"daily_minutes": 120}
    guest = next(u for u in policy.effective_users() if u.uid == 1010)
    assert guest.time["allowed"] == tl.SCHEDULE_PRESETS["not_late"]
    assert Policy.from_dict(policy.to_dict()).to_dict() == policy.to_dict()


def test_the_schema_refuses_a_malformed_grid():
    from kosherd.policy import PolicyError

    doc = Policy(revision=1, users=[UserPolicy(
        uid=1001, username="yosef", mode="filtered",
        time={"allowed": ["1" * 24] * 7})]).to_dict()
    doc["users"][0]["time"] = {"allowed": ["1" * 24] * 6 + ["x" * 24]}
    with pytest.raises(PolicyError):
        Policy.from_dict(doc)
    doc["users"][0]["time"] = {"daily_minutes": 5000}
    with pytest.raises(PolicyError):
        Policy.from_dict(doc)


# -- the clock -----------------------------------------------------------------

def test_the_schedule_is_read_by_local_weekday_and_hour():
    grid = tl.SCHEDULE_PRESETS["weekdays"]
    assert tl.allowed_at(grid, time.localtime(MONDAY_10))
    assert not tl.allowed_at(grid, time.localtime(SATURDAY_10))
    after = tl.SCHEDULE_PRESETS["after_school"]
    assert not tl.allowed_at(after, time.localtime(MONDAY_10))
    assert tl.allowed_at(after, time.localtime(local(2026, 9, 14, 15)))
    assert not tl.allowed_at(after, time.localtime(local(2026, 9, 14, 20)))


def test_when_the_current_block_ends():
    not_late = tl.SCHEDULE_PRESETS["not_late"]       # 06:00-21:00 every day
    now = local(2026, 9, 14, 19, 30)
    assert tl.block_end(not_late, now) == local(2026, 9, 14, 21)
    assert tl.block_end([tl.ALWAYS] * 7, now) is None, "nothing ends when always allowed"
    late = local(2026, 9, 14, 22)
    assert tl.block_end(not_late, late) == late, "not allowed now: the block ended already"


def test_when_a_blocked_account_may_sign_in_again():
    not_late = tl.SCHEDULE_PRESETS["not_late"]
    late = local(2026, 9, 14, 22)
    assert tl.next_allowed(not_late, late) == local(2026, 9, 15, 6)
    assert tl.next_allowed(not_late, MONDAY_10) == MONDAY_10
    weekdays = tl.SCHEDULE_PRESETS["weekdays"]
    assert tl.next_allowed(weekdays, SATURDAY_10) == local(2026, 9, 21, 0), "Monday midnight"
    assert tl.next_allowed([tl.NEVER] * 7, MONDAY_10) is None


def test_status_says_what_ends_the_session_first():
    two_hours_not_late = {"daily_minutes": 120, "allowed": tl.SCHEDULE_PRESETS["not_late"]}
    now = local(2026, 9, 14, 19, 30)
    # 30 min of budget left, 90 min of block left: the budget ends it.
    s = tl.status(two_hours_not_late, used=90 * 60, now=now)
    assert s["left"] == 30 * 60 and s["reason"] == "limit"
    assert s["allowed_now"] and not s["blocked"] and s["limited"]
    assert s["block_ends"] == local(2026, 9, 14, 21)
    # 100 min of budget, 90 of block: the block ends it.
    s = tl.status(two_hours_not_late, used=20 * 60, now=now)
    assert s["left"] == 90 * 60 and s["reason"] == "schedule"
    # Budget spent: blocked, and the reason is the limit.
    s = tl.status(two_hours_not_late, used=120 * 60, now=now)
    assert s["blocked"] and s["left"] == 0 and s["reason"] == "limit"
    # Outside the hours: blocked, says when it may come back.
    s = tl.status(two_hours_not_late, used=0, now=local(2026, 9, 14, 22))
    assert s["blocked"] and s["reason"] == "schedule"
    assert s["next_allowed"] == local(2026, 9, 15, 6) and s["block_ends"] is None


def test_status_of_an_unlimited_account_has_nothing_to_end_it():
    s = tl.status({}, used=3600, now=MONDAY_10)
    assert s["left"] is None and s["reason"] is None and not s["blocked"]
    assert not s["limited"] and s["used"] == 3600 and s["limit"] == 0


# -- words ---------------------------------------------------------------------

def test_durations_read_like_a_person_says_them():
    assert tl.duration_text(0) == "0 min"
    assert tl.duration_text(45 * 60) == "45 min"
    assert tl.duration_text(2 * 3600) == "2 h"
    assert tl.duration_text(80 * 60) == "1 h 20 min"
    assert tl.duration_text(89) == "1 min"


def test_a_days_hours_read_as_clock_ranges():
    assert tl.hours_text(tl.ALWAYS) == "All day"
    assert tl.hours_text(tl.NEVER) == "Not at all"
    assert tl.hours_text(tl._hours(7, 21)) == "07:00–21:00"
    split = "".join("1" if 7 <= h < 9 or 15 <= h < 21 else "0" for h in range(24))
    assert tl.hours_text(split) == "07:00–09:00, 15:00–21:00"
    assert tl.hours_text("0" * 23 + "1") == "23:00–24:00"


# -- pam_time ------------------------------------------------------------------

def test_pam_time_lines_group_days_with_the_same_hours():
    assert tl.pam_times([tl.ALWAYS] * 7) == ""
    assert tl.pam_times(tl.SCHEDULE_PRESETS["not_late"]) == "MoTuWeThFrSaSu0600-2100"
    assert tl.pam_times(tl.SCHEDULE_PRESETS["after_school"]) == \
        "MoTuWeThFr1500-2000|SaSu0900-2000"
    assert tl.pam_times(tl.SCHEDULE_PRESETS["weekdays"]) == "MoTuWeThFr0000-2400"
    assert tl.pam_times([tl.NEVER] * 7) == "!Al0000-2400"
    split = "".join("1" if 7 <= h < 9 or 15 <= h < 21 else "0" for h in range(24))
    assert tl.pam_times([split] + [tl.NEVER] * 6) == "Mo0700-0900|Mo1500-2100"


def _family():
    return Policy(revision=1, users=[
        UserPolicy(uid=1000, username="abba", mode="filtered", admin=True,
                   time={"daily_minutes": 30}),
        UserPolicy(uid=1001, username="yosef", mode="filtered",
                   time={"daily_minutes": 120, "allowed": tl.SCHEDULE_PRESETS["not_late"]}),
        UserPolicy(uid=1002, username="rivky", mode="filtered", time={"daily_minutes": 180}),
        UserPolicy(uid=1003, username="free", mode="unfiltered"),
    ])


def test_time_conf_names_only_accounts_with_a_schedule_or_no_time_left():
    conf = tl.render_time_conf(_family())
    assert "*;*;yosef;MoTuWeThFrSaSu0600-2100" in conf
    assert "abba" not in conf, "administrators are never limited"
    assert "rivky" not in conf, "a daily limit alone is not a sign-in schedule"
    assert "free" not in conf
    exhausted = tl.render_time_conf(_family(), exhausted={1002})
    assert "*;*;rivky;!Al0000-2400" in exhausted
    assert "*;*;yosef;MoTuWeThFrSaSu0600-2100" in exhausted
    # An admin whose budget is somehow marked spent is still never named.
    assert "abba" not in tl.render_time_conf(_family(), exhausted={1000})


def test_time_conf_is_written_atomically_and_world_readable(tmp_path):
    path = tmp_path / "time.conf"
    tl.write_time_conf(_family(), {1002}, path=path)
    assert path.read_text().startswith("# Written by kosherd")
    assert "rivky;!Al0000-2400" in path.read_text()
    assert oct(path.stat().st_mode & 0o777) == "0o644"
    assert not (tmp_path / "time.conf.tmp").exists()


# -- the day's budget ----------------------------------------------------------

def test_usage_survives_a_restart_and_resets_at_midnight(tmp_path):
    path = tmp_path / "usage.jsonl"
    store = tl.UsageStore(path)
    store.add(1001, 60, MONDAY_10)
    store.add(1001, 60, MONDAY_10 + 60)
    store.add(1002, 60, MONDAY_10)
    assert store.used_today(1001, MONDAY_10 + 120) == 120
    # A new process reads the same numbers back.
    again = tl.UsageStore(path)
    assert again.used_today(1001, MONDAY_10 + 120) == 120
    assert again.used_today(1002, MONDAY_10 + 120) == 60
    # Tuesday starts from nothing.
    assert again.used_today(1001, local(2026, 9, 15, 0, 1)) == 0
    assert oct(path.stat().st_mode & 0o777) == "0o600"


def test_usage_ignores_torn_lines_and_nothing_and_trims_old_days(tmp_path):
    path = tmp_path / "usage.jsonl"
    store = tl.UsageStore(path)
    assert store.used_today(1001, MONDAY_10) == 0
    store.add(1001, 0, MONDAY_10)          # nothing to note
    store.add(1001, 60, MONDAY_10 - 3 * 86400)
    store.add(1001, 60, MONDAY_10)
    with open(path, "a") as handle:
        handle.write('{"t": 1, "uid"\n')
    fresh = tl.UsageStore(path)
    assert fresh.used_today(1001, MONDAY_10) == 60
    fresh.trim(MONDAY_10)
    lines = [json.loads(l) for l in path.read_text().splitlines()]
    assert [l["t"] for l in lines] == [int(MONDAY_10)]
    fresh.trim(MONDAY_10)  # nothing more to drop: no rewrite, no error


def test_usage_in_an_unwritable_place_is_a_warning_not_a_crash(tmp_path):
    store = tl.UsageStore(tmp_path / "nowhere" / "deeper" / "usage.jsonl")
    store.add(1001, 60, MONDAY_10)   # the directory is created
    assert store.used_today(1001, MONDAY_10) == 60
    blocked = tl.UsageStore(tmp_path / "usage.jsonl" / "impossible")
    (tmp_path / "usage.jsonl").write_text("")
    blocked.add(1001, 60, MONDAY_10)  # the parent is a file; logged, not raised
    assert blocked.used_today(1001, MONDAY_10) == 60, "the minute still counts in memory"


# -- a sign-in pam_time refused --------------------------------------------------

def test_a_refused_sign_in_is_written_to_the_activity_log(tmp_path):
    class Entry:
        pw_uid = 1001

    ok = tl.record_refused_login({"PAM_USER": "yosef", "PAM_SERVICE": "gdm-password"},
                                 spool=tmp_path, getpwnam=lambda name: Entry())
    assert ok
    [event] = activity.events(spool=tmp_path)
    assert event["kind"] == activity.TIME and event["uid"] == 1001
    assert event["why"] == tl.WHY_LOGIN and event["service"] == "gdm-password"


def test_a_refused_sign_in_for_nobody_writes_nothing(tmp_path):
    def missing(name):
        raise KeyError(name)

    assert not tl.record_refused_login({}, spool=tmp_path)
    assert not tl.record_refused_login({"PAM_USER": "ghost"}, spool=tmp_path,
                                       getpwnam=missing)
    assert activity.events(spool=tmp_path) == []
