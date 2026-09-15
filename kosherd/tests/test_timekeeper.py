"""The minute-by-minute enforcer: count, warn, lock, sign out, refuse.

Driven with a fake clock and fake logind sessions, so the whole sequence a
child would live through — warned at 15, warned at 5, told it is over,
locked, signed out, refused at the login screen — runs in a second.
"""

import time

from kosherd import timekeeper as tk
from kosherd import timelimits as tl
from kosherd.policy import Policy, UserPolicy


def local(year, month, day, hour, minute=0):
    return time.mktime((year, month, day, hour, minute, 0, 0, 0, -1))


MONDAY_10 = local(2026, 9, 14, 10)   # a Monday
YOSEF, RIVKY, ABBA = 1001, 1002, 1000


class Rig:
    """A Timekeeper wired to recorders, with knobs for the clock, the
    sessions logind reports and the policy."""

    def __init__(self, tmp_path, users, start=MONDAY_10):
        self.now = start
        self.users = users
        self.sessions = []
        self.warned, self.locked, self.terminated, self.recorded, self.confs = [], [], [], [], []
        self.keeper = tk.Timekeeper(
            lambda: Policy(revision=1, users=self.users),
            lambda: list(self.sessions),
            tl.UsageStore(tmp_path / "usage.jsonl"),
            warn=lambda uid, minutes, reason: self.warned.append((uid, minutes, reason)),
            lock=self.locked.append, terminate=self.terminated.append,
            write_conf=lambda policy, exhausted: self.confs.append(set(exhausted)),
            record=lambda uid, why: self.recorded.append((uid, why)),
            clock=lambda: self.now)

    def signed_in(self, *uids, idle=False):
        self.sessions = [tk.Session(id=f"s{uid}", uid=uid, idle=idle) for uid in uids]

    def run(self, minutes=1):
        for _ in range(minutes):
            self.now += 60
            self.keeper.tick()

    def used(self, uid):
        return self.keeper.usage.used_today(uid, self.now)


def two_hours(uid=YOSEF, name="yosef", **time_settings):
    return UserPolicy(uid=uid, username=name, mode="filtered",
                      time={"daily_minutes": 120, **time_settings})


# -- counting ----------------------------------------------------------------

def test_active_minutes_count_and_idle_ones_do_not(tmp_path):
    rig = Rig(tmp_path, [two_hours()])
    rig.keeper.tick()                      # the first tick only takes the time
    rig.signed_in(YOSEF)
    rig.run(10)
    assert rig.used(YOSEF) == 10 * 60
    rig.signed_in(YOSEF, idle=True)
    rig.run(10)
    assert rig.used(YOSEF) == 10 * 60, "idle time is free"
    rig.sessions = []
    rig.run(5)
    assert rig.used(YOSEF) == 10 * 60, "signed-out time is free"


def test_a_late_tick_does_not_charge_the_sleep(tmp_path):
    rig = Rig(tmp_path, [two_hours()])
    rig.signed_in(YOSEF)
    rig.keeper.tick()
    rig.now += 3600                        # the lid was closed for an hour
    rig.keeper.tick()
    assert rig.used(YOSEF) == tk.MAX_CHARGE_SECONDS


def test_administrators_and_unlimited_accounts_are_never_counted_or_touched(tmp_path):
    rig = Rig(tmp_path, [
        UserPolicy(uid=ABBA, username="abba", mode="filtered", admin=True,
                   time={"daily_minutes": 1}),
        UserPolicy(uid=RIVKY, username="rivky", mode="filtered"),
    ])
    rig.signed_in(ABBA, RIVKY)
    rig.run(5)
    assert rig.used(ABBA) == 0 and rig.used(RIVKY) == 0
    assert not rig.warned and not rig.locked and not rig.terminated


# -- warnings ----------------------------------------------------------------

def test_warned_at_fifteen_and_five_minutes_then_told_it_is_over(tmp_path):
    rig = Rig(tmp_path, [two_hours()])
    rig.keeper.usage.add(YOSEF, 104 * 60, rig.now)     # 16 min left
    rig.signed_in(YOSEF)
    rig.keeper.tick()
    assert rig.warned == []
    rig.run(1)                                           # 15 min left
    assert rig.warned == [(YOSEF, 15, "limit")]
    rig.run(9)                                           # 6 min left
    assert len(rig.warned) == 1, "one warning per threshold"
    rig.run(1)                                           # 5 min left
    assert rig.warned[-1] == (YOSEF, 5, "limit")
    rig.run(5)                                           # 0 left
    assert rig.warned[-1] == (YOSEF, 0, "limit")
    assert rig.locked == [YOSEF]
    assert rig.recorded == [(YOSEF, tl.WHY_LIMIT)]
    assert rig.terminated == []
    rig.run(1)                                           # the grace minute
    assert rig.terminated == [YOSEF]


def test_a_warning_is_only_for_somebody_who_is_signed_in(tmp_path):
    rig = Rig(tmp_path, [two_hours()])
    rig.keeper.usage.add(YOSEF, 110 * 60, rig.now)
    rig.run(1)
    assert rig.warned == [], "nobody at the keyboard to warn"
    rig.signed_in(YOSEF)
    rig.run(1)
    assert rig.warned == [(YOSEF, 9, "limit")], "one minute was charged after the sign-in"


def test_loosening_the_limit_puts_the_warnings_back(tmp_path):
    rig = Rig(tmp_path, [two_hours()])
    rig.keeper.usage.add(YOSEF, 106 * 60, rig.now)
    rig.signed_in(YOSEF)
    rig.run(1)
    assert rig.warned == [(YOSEF, 14, "limit")]
    rig.users = [UserPolicy(uid=YOSEF, username="yosef", mode="filtered",
                            time={"daily_minutes": 180})]
    rig.keeper.policy_changed()
    rig.run(1)
    assert len(rig.warned) == 1
    rig.keeper.usage.add(YOSEF, 60 * 60, rig.now)    # back down to ~13 min
    rig.run(1)
    assert len(rig.warned) == 2, "the 15-minute warning fires again"


def test_the_allowed_hours_ending_is_warned_about_too(tmp_path):
    user = UserPolicy(uid=YOSEF, username="yosef", mode="filtered",
                      time={"allowed": tl.SCHEDULE_PRESETS["not_late"]})  # until 21:00
    rig = Rig(tmp_path, [user], start=local(2026, 9, 14, 20, 44))
    rig.signed_in(YOSEF)
    rig.keeper.tick()
    rig.run(1)                                            # 20:45 → 15 min
    assert rig.warned == [(YOSEF, 15, "schedule")]
    rig.run(10)                                           # 20:55
    assert rig.warned[-1] == (YOSEF, 5, "schedule")
    rig.run(5)                                            # 21:00
    assert rig.warned[-1] == (YOSEF, 0, "schedule")
    assert rig.locked == [YOSEF] and rig.recorded == [(YOSEF, tl.WHY_SCHEDULE)]
    rig.run(1)
    assert rig.terminated == [YOSEF]


# -- refusing ----------------------------------------------------------------

def test_a_sign_in_while_blocked_is_ended_at_once_and_logged(tmp_path):
    rig = Rig(tmp_path, [two_hours()])
    rig.keeper.usage.add(YOSEF, 120 * 60, rig.now)
    rig.run(1)                                            # blocked, nobody in
    assert not rig.locked and not rig.terminated
    rig.signed_in(YOSEF)
    rig.run(1)
    assert rig.terminated == [YOSEF]
    assert rig.recorded == [(YOSEF, tl.WHY_LOGIN)]
    assert rig.locked == [], "no grace for a sign-in that should not have happened"


def test_a_signed_out_person_is_not_terminated_again(tmp_path):
    rig = Rig(tmp_path, [two_hours()])
    rig.keeper.usage.add(YOSEF, 120 * 60, rig.now)
    rig.signed_in(YOSEF)
    rig.run(2)
    assert rig.terminated == [YOSEF]
    rig.sessions = []
    rig.run(5)
    assert rig.terminated == [YOSEF]


def test_the_sign_in_schedule_is_rewritten_when_it_changes_not_every_minute(tmp_path):
    rig = Rig(tmp_path, [two_hours()])
    rig.run(3)
    assert rig.confs == [set()]
    rig.keeper.usage.add(YOSEF, 120 * 60, rig.now)
    rig.run(1)
    assert rig.confs == [set(), {YOSEF}], "spent: the account is now refused"
    rig.run(3)
    assert len(rig.confs) == 2
    rig.now = local(2026, 9, 15, 0, 1)                    # a new day
    rig.keeper.tick()
    assert rig.confs[-1] == set(), "the budget is back, so is the sign-in"


def test_a_failed_write_of_the_schedule_is_logged_not_fatal(tmp_path):
    def broken(policy, exhausted):
        raise OSError("read-only")

    rig = Rig(tmp_path, [two_hours()])
    rig.keeper._write_conf = broken
    rig.run(1)   # no exception


def test_the_real_writer_is_the_default(tmp_path, monkeypatch):
    monkeypatch.setattr(tl, "TIME_CONF", tmp_path / "time.conf")
    keeper = tk.Timekeeper(
        lambda: Policy(revision=1, users=[two_hours(allowed=tl.SCHEDULE_PRESETS["weekdays"])]),
        lambda: [], tl.UsageStore(tmp_path / "usage.jsonl"),
        warn=lambda *a: None, lock=lambda uid: None, terminate=lambda uid: None,
        write_conf=lambda policy, exhausted: tl.write_time_conf(
            policy, exhausted, path=tmp_path / "time.conf"),
        clock=lambda: MONDAY_10)
    keeper.tick()
    assert "yosef;MoTuWeThFr0000-2400" in (tmp_path / "time.conf").read_text()


# -- what the apps are shown ----------------------------------------------------

def test_the_snapshot_says_where_everyone_stands(tmp_path):
    rig = Rig(tmp_path, [
        UserPolicy(uid=ABBA, username="abba", mode="filtered", admin=True),
        two_hours(),
        UserPolicy(uid=RIVKY, username="rivky", mode="filtered"),
    ])
    rig.keeper.tick()
    rig.signed_in(YOSEF)
    rig.run(30)
    snap = rig.keeper.snapshot()
    assert snap["1000"] == {"admin": True, "limited": False, "used": 0, "signed_in": False}
    yosef = snap["1001"]
    assert yosef["used"] == 30 * 60 and yosef["limit"] == 120 * 60
    assert yosef["left"] == 90 * 60 and yosef["signed_in"] and yosef["limited"]
    assert snap["1002"]["limited"] is False and snap["1002"]["left"] is None
    assert rig.keeper.status_for(YOSEF)["used"] == 30 * 60
    assert rig.keeper.status_for(4242) is None


def test_the_snapshot_survives_logind_not_answering(tmp_path):
    rig = Rig(tmp_path, [two_hours()])

    def broken():
        raise RuntimeError("no bus")

    rig.keeper._sessions = broken
    assert rig.keeper.snapshot()["1001"]["signed_in"] is False


def test_the_default_recorder_writes_the_activity_log(monkeypatch):
    written = []
    monkeypatch.setattr(tk.activity, "record",
                        lambda *args, **kw: written.append((args, kw)) or True)
    tk.Timekeeper._record_activity(YOSEF, tl.WHY_LIMIT)
    assert written == [(("kosherd", tk.activity.TIME, YOSEF), {"why": tl.WHY_LIMIT})]
