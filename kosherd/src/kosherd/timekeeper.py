"""The clock that ends a session: counting, warning, locking, signing out.

kosherd runs one tick a minute. Each tick asks logind who is signed in and
whether they are idle, adds the elapsed minute to every active, limited
account's budget, and works out how long each has left — until the daily
limit runs out or the allowed hours end, whichever comes first. Then:

- at 15 and at 5 minutes left, a TimeWarning is sent (the person's
  session shows it as a desktop notification, see timenotify.py);
- when the time is up, a last warning, the sessions are locked, the event
  goes in the activity log, and /etc/security/time.conf is rewritten so
  pam_time refuses a fresh sign-in;
- one tick later, if the person is still signed in, the sessions are ended.

A session that appears while the account is blocked — a sign-in that got
past the PAM gate, a console login — is ended at once and logged as a
refused sign-in. Idle time (logind's IdleHint) does not count against the
budget. Administrators are never touched.

Everything that talks to the system is injected, so the whole sequence is
exercised by tests with a fake clock and fake sessions; the daemon binds
the real logind calls and D-Bus signal.
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass

from . import activity, timelimits

log = logging.getLogger(__name__)

TICK_SECONDS = 60
# A tick that arrives late (the machine slept) must not charge the sleep.
MAX_CHARGE_SECONDS = 2 * TICK_SECONDS
# Minutes left at which the person is warned; the most urgent first.
WARN_AT = (15 * 60, 5 * 60)
# Between the lock and the sign-out: long enough for the notification to
# be read, short enough that unlocking again buys nothing worth having.
GRACE_SECONDS = 60


@dataclass(frozen=True)
class Session:
    """What the enforcer needs to know about one logind session."""

    id: str
    uid: int
    active: bool = True
    idle: bool = False


class Timekeeper:
    def __init__(self, policy_getter, sessions_getter, usage: timelimits.UsageStore,
                 *, warn, lock, terminate, write_conf=None, record=None,
                 clock=time.time):
        self._policy = policy_getter
        self._sessions = sessions_getter
        self.usage = usage
        self._warn = warn            # (uid, minutes_left, reason)
        self._lock = lock            # (uid)
        self._terminate = terminate  # (uid)
        self._write_conf = write_conf or timelimits.write_time_conf  # (policy, exhausted)
        self._record = record or self._record_activity                # (uid, why)
        self._clock = clock
        self._last_tick: float | None = None
        self._warned: dict[int, set[int]] = {}
        self._locked_at: dict[int, float] = {}
        self._blocked: set[int] = set()
        self._seen_sessions: set[str] = set()
        self._conf_key: tuple | None = None

    # -- one minute ------------------------------------------------------

    def tick(self, now: float | None = None) -> None:
        now = self._clock() if now is None else now
        elapsed = 0
        if self._last_tick is not None:
            elapsed = int(min(max(0.0, now - self._last_tick), MAX_CHARGE_SECONDS))
        self._last_tick = now

        policy = self._policy()
        sessions = list(self._sessions())
        ids = {s.id for s in sessions}
        new_ids = ids - self._seen_sessions
        self._seen_sessions = ids
        present = {s.uid for s in sessions}
        active = {s.uid for s in sessions if s.active and not s.idle}

        exhausted: set[int] = set()
        blocked_now: set[int] = set()
        for user in policy.effective_users():
            if user.admin or not timelimits.is_limited(user.time):
                self._forget(user.uid)
                continue
            if user.uid in active and elapsed:
                self.usage.add(user.uid, elapsed, now)
            state = timelimits.status(user.time, self.usage.used_today(user.uid, now), now)
            if state["limit"] and state["used"] >= state["limit"]:
                exhausted.add(user.uid)
            if state["blocked"]:
                blocked_now.add(user.uid)
                self._enforce(user.uid, state, sessions, new_ids, present, now)
                continue
            self._locked_at.pop(user.uid, None)
            self._maybe_warn(user.uid, state, present)

        self._blocked = blocked_now
        key = (policy.revision, frozenset(exhausted),
               tuple(sorted((u.uid, u.username) for u in policy.effective_users())))
        if key != self._conf_key:
            self._conf_key = key
            try:
                self._write_conf(policy, exhausted)
            except OSError as e:
                log.error("could not write the sign-in schedule: %s", e)
        if int(now) % 3600 < TICK_SECONDS:  # once an hour is plenty
            self.usage.trim(now)

    def _enforce(self, uid: int, state: dict, sessions, new_ids, present, now) -> None:
        why = timelimits.WHY_LIMIT if state["reason"] == "limit" else timelimits.WHY_SCHEDULE
        if uid not in present:
            self._locked_at.pop(uid, None)
            return
        arrived = [s for s in sessions if s.uid == uid and s.id in new_ids]
        if uid in self._blocked and arrived and len(arrived) == len(
                [s for s in sessions if s.uid == uid]):
            # Every session this person has appeared while they were already
            # blocked: a sign-in the gate should have refused. No grace.
            self._record(uid, timelimits.WHY_LOGIN)
            self._terminate(uid)
            return
        locked = self._locked_at.get(uid)
        if locked is None:
            self._warn(uid, 0, state["reason"])
            self._record(uid, why)
            self._lock(uid)
            self._locked_at[uid] = now
        elif now - locked >= GRACE_SECONDS:
            self._terminate(uid)

    def _maybe_warn(self, uid: int, state: dict, present) -> None:
        left = state["left"]
        if left is None:
            self._warned.pop(uid, None)
            return
        warned = self._warned.setdefault(uid, set())
        # A limit that was loosened, or a new day, puts the warnings back.
        warned.intersection_update({t for t in WARN_AT if left <= t})
        if uid not in present:
            return
        for threshold in WARN_AT:
            if left <= threshold and threshold not in warned:
                warned.add(threshold)
                self._warn(uid, max(1, math.ceil(left / 60)), state["reason"])
                break

    def _forget(self, uid: int) -> None:
        self._warned.pop(uid, None)
        self._locked_at.pop(uid, None)

    # -- the rest of the daemon ------------------------------------------

    def policy_changed(self) -> None:
        """The policy was saved: re-render the sign-in schedule now rather
        than at the next tick, so a change in the admin app is live at once."""
        self._conf_key = None
        self.tick()

    def snapshot(self, now: float | None = None) -> dict[str, dict]:
        """Where every managed account stands, keyed by uid as a string,
        for the admin app's cards and the person's page."""
        now = self._clock() if now is None else now
        try:
            present = {s.uid for s in self._sessions()}
        except Exception:  # noqa: BLE001 - a read for display must not fail the call
            present = set()
        out = {}
        for user in self._policy().effective_users():
            if user.admin:
                out[str(user.uid)] = {"admin": True, "limited": False, "used": 0,
                                      "signed_in": user.uid in present}
                continue
            state = timelimits.status(user.time, self.usage.used_today(user.uid, now), now)
            state["signed_in"] = user.uid in present
            out[str(user.uid)] = state
        return out

    def status_for(self, uid: int, now: float | None = None) -> dict | None:
        """One account's standing, for GetMySettings."""
        return self.snapshot(now).get(str(uid))

    @staticmethod
    def _record_activity(uid: int, why: str) -> None:
        activity.record("kosherd", activity.TIME, uid, why=why)
