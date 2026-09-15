"""How long, and when, an account may be signed in.

Two settings, both per account and both off by default:

- a daily limit ("2 hours a day") of ACTIVE signed-in time — minutes the
  person is at the keyboard, not minutes the screen sat idle;
- a weekly schedule: for each hour of each day, whether the account may
  be signed in at all. A parent paints it on a calendar in the admin app.

Neither is a network setting, so the enforcement is the daemon's own
(see timekeeper.py): it counts the minutes, warns the person at 15 and 5
minutes, locks and ends the session when the time is up, and renders
/etc/security/time.conf so pam_time refuses a new sign-in until the account
is allowed again. Administrators are never limited — a parent must always
be able to sign in to change a setting.

This module is the pure half: the shape of the settings, the clock
arithmetic, the pam_time rendering and the words for a duration. Nothing
here touches D-Bus or logind.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

log = logging.getLogger(__name__)

HOURS = 24
DAYS = 7
ALWAYS = "1" * HOURS
NEVER = "0" * HOURS
# Monday first, as Python's weekday() counts; the admin app displays the
# week Sunday-first and maps the columns.
DAY_KEYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
PAM_DAYS = ("Mo", "Tu", "We", "Th", "Fr", "Sa", "Su")

# The one-click daily limits the admin app offers; 0 is "no limit". Words
# for them live in the app's label table, which a test keeps in step.
LIMIT_PRESET_MINUTES = (30, 60, 120, 180, 0)
MAX_DAILY_MINUTES = 1440


def _days(weekdays: str, weekends: str) -> list[str]:
    return [weekdays] * 5 + [weekends] * 2


def _hours(start: int, end: int) -> str:
    """'1' from `start` up to but not including `end`, '0' elsewhere."""
    return "".join("1" if start <= h < end else "0" for h in range(HOURS))


# Starting points a parent picks with one click and then paints on. Keys
# are stable (the app's labels and the tests name them); the grids are
# Monday-first like the policy.
SCHEDULE_PRESETS = {
    "always": [ALWAYS] * DAYS,
    # School-day afternoons and evenings, longer at the weekend.
    "after_school": _days(_hours(15, 20), _hours(9, 20)),
    # Any time except the night.
    "not_late": [_hours(6, 21)] * DAYS,
    "weekdays": _days(ALWAYS, NEVER),
}

TIME_CONF = Path("/etc/security/time.conf")
# Where the daily budget is kept, so a reboot does not hand the day back.
USAGE_DIR = Path("/var/lib/kosher-time")
USAGE_PATH = USAGE_DIR / "usage.jsonl"
USAGE_KEEP_SECONDS = 2 * 24 * 3600

# Why a session ended or a sign-in was refused, as the activity log records
# it (activity.TIME events, `why` field).
WHY_LIMIT = "time:limit"        # the daily budget ran out
WHY_SCHEDULE = "time:schedule"  # the allowed hours ended
WHY_LOGIN = "time:login"        # a sign-in outside the allowed time was refused


class TimeError(ValueError):
    """The settings are not a shape the policy accepts."""


# -- the settings ------------------------------------------------------------

def parse(doc) -> dict:
    """Check and tidy a time-settings document; raises TimeError.

    Returns the compact form the policy stores: defaults are dropped, so an
    account with no limit and no schedule is `{}` — invisible in the file
    and in the admin app's drift view.
    """
    if doc is None:
        return {}
    if not isinstance(doc, dict):
        raise TimeError("time settings must be an object")
    unknown = set(doc) - {"daily_minutes", "allowed"}
    if unknown:
        raise TimeError(f"unknown time setting(s): {', '.join(sorted(unknown))}")
    out: dict = {}
    minutes = doc.get("daily_minutes", 0)
    if isinstance(minutes, bool) or not isinstance(minutes, int):
        raise TimeError("daily_minutes must be a whole number of minutes")
    if not 0 <= minutes <= MAX_DAILY_MINUTES:
        raise TimeError(f"daily_minutes must be between 0 and {MAX_DAILY_MINUTES}")
    if minutes:
        out["daily_minutes"] = minutes
    allowed = doc.get("allowed")
    if allowed is not None:
        if not isinstance(allowed, list) or len(allowed) != DAYS:
            raise TimeError(f"allowed must list {DAYS} days")
        grid = []
        for day in allowed:
            if not isinstance(day, str) or len(day) != HOURS or set(day) - {"0", "1"}:
                raise TimeError(f"each day must be {HOURS} characters of 0 or 1")
            grid.append(day)
        if not is_always(grid):
            out["allowed"] = grid
    return out


def daily_minutes(settings: dict | None) -> int:
    return int((settings or {}).get("daily_minutes", 0) or 0)


def grid(settings: dict | None) -> list[str]:
    """The weekly schedule, Monday first; always allowed when unset."""
    allowed = (settings or {}).get("allowed")
    return list(allowed) if allowed else [ALWAYS] * DAYS


def is_always(schedule: list[str]) -> bool:
    return all(day == ALWAYS for day in schedule)


def is_limited(settings: dict | None) -> bool:
    """Whether anything here can ever end a session."""
    return bool(daily_minutes(settings)) or not is_always(grid(settings))


# -- the clock ---------------------------------------------------------------

def allowed_at(schedule: list[str], when: time.struct_time) -> bool:
    return schedule[when.tm_wday][when.tm_hour] == "1"


def _hour_start(now: float) -> float:
    t = time.localtime(now)
    return time.mktime((t.tm_year, t.tm_mon, t.tm_mday, t.tm_hour, 0, 0, 0, 0, -1))


def _walk(schedule: list[str], now: float, want: str) -> float | None:
    """The start of the first hour from now whose state is `want`.

    Steps hour by hour from the current hour's start and re-reads the local
    time at each step, so a clock change overnight does not shift the grid.
    Returns None if no such hour exists within a week (the grid repeats).
    """
    start = _hour_start(now)
    for k in range(DAYS * HOURS + 1):
        t = start + k * 3600
        if schedule[time.localtime(t).tm_wday][time.localtime(t).tm_hour] == want:
            return t
    return None


def block_end(schedule: list[str], now: float) -> float | None:
    """When the allowed block the account is in right now ends.

    None when the account is always allowed (nothing ends); `now` itself
    when it is not allowed at this moment.
    """
    if is_always(schedule):
        return None
    if not allowed_at(schedule, time.localtime(now)):
        return now
    return _walk(schedule, now, "0")


def next_allowed(schedule: list[str], now: float) -> float | None:
    """When the account may next sign in; `now` if it may right now, None
    if the schedule allows nothing at all."""
    if allowed_at(schedule, time.localtime(now)):
        return now
    return _walk(schedule, now, "1")


def status(settings: dict | None, used: int, now: float | None = None) -> dict:
    """Where an account stands right now, for the apps and the enforcer.

    used     seconds of active time so far today
    limit    the daily budget in seconds, 0 for none
    left     seconds until something ends the session, or None if nothing will
    reason   what ends it first: "limit" or "schedule" (None if nothing)
    allowed_now  whether the schedule allows this hour
    blocked  whether the account may not be signed in right now
    block_ends   when the current allowed block ends, or None
    next_allowed when a blocked account may sign in again, or None
    """
    now = time.time() if now is None else now
    minutes = daily_minutes(settings)
    schedule = grid(settings)
    limit = minutes * 60
    budget_left = max(0, limit - used) if limit else None
    allowed_now = allowed_at(schedule, time.localtime(now))
    ends = block_end(schedule, now)
    block_left = None if ends is None else max(0, int(ends - now))

    left, reason = None, None
    if budget_left is not None:
        left, reason = budget_left, "limit"
    if block_left is not None and (left is None or block_left < left):
        left, reason = block_left, "schedule"
    blocked = (budget_left == 0) or not allowed_now
    if budget_left == 0:
        reason = "limit"
    elif not allowed_now:
        reason = "schedule"
    return {
        "used": int(used), "limit": limit, "left": left, "reason": reason,
        "allowed_now": allowed_now, "blocked": blocked,
        "block_ends": (None if ends is None or not allowed_now else int(ends)),
        "next_allowed": (None if allowed_now else
                         (lambda t: None if t is None else int(t))(
                             next_allowed(schedule, now))),
        "limited": is_limited(settings),
    }


# -- words -------------------------------------------------------------------

def duration_text(seconds: int | float) -> str:
    """'1 h 20 min', '45 min', '2 h', '0 min'."""
    minutes = max(0, int(round(seconds / 60)))
    hours, rest = divmod(minutes, 60)
    if hours and rest:
        return f"{hours} h {rest} min"
    if hours:
        return f"{hours} h"
    return f"{rest} min"


def hours_text(day: str) -> str:
    """A day's allowed hours as clock ranges: '07:00–21:00', 'All day',
    'Not at all'; several ranges joined with commas."""
    if day == ALWAYS:
        return "All day"
    if day == NEVER:
        return "Not at all"
    ranges = []
    start = None
    for hour, flag in enumerate(day + "0"):
        if flag == "1" and start is None:
            start = hour
        elif flag == "0" and start is not None:
            ranges.append(f"{start:02d}:00–{hour:02d}:00")
            start = None
    return ", ".join(ranges)


# -- pam_time ----------------------------------------------------------------

def _pam_ranges(day: str) -> list[str]:
    """'HHMM-HHMM' for each run of allowed hours in a day string."""
    ranges = []
    start = None
    for hour, flag in enumerate(day + "0"):
        if flag == "1" and start is None:
            start = hour
        elif flag == "0" and start is not None:
            ranges.append(f"{start:02d}00-{hour:02d}00")
            start = None
    return ranges


def pam_times(schedule: list[str]) -> str:
    """A pam_time day/time list for a weekly grid, or '' when it allows
    everything (no line is written for such an account)."""
    if is_always(schedule):
        return ""
    by_pattern: dict[str, list[int]] = {}
    for index, day in enumerate(schedule):
        if day != NEVER:
            by_pattern.setdefault(day, []).append(index)
    if not by_pattern:
        return "!Al0000-2400"
    entries = []
    for pattern, indexes in by_pattern.items():
        days = "".join(PAM_DAYS[i] for i in indexes)
        entries.extend(f"{days}{span}" for span in _pam_ranges(pattern))
    return "|".join(entries)


def render_time_conf(policy, exhausted=()) -> str:
    """/etc/security/time.conf: when each limited account may sign in.

    One line per account with a schedule, in pam_time's
    `services;ttys;users;times` form; an account whose daily budget is used
    up (`exhausted`, by uid) gets a line refusing every hour, replaced at
    the next render once a new day starts. Accounts not named are allowed,
    which is pam_time's default and ours: administrators and unlimited
    accounts never appear.
    """
    lines = [
        "# Written by kosherd from the device policy; edits are overwritten.",
        "# services;ttys;users;times — an account not named here may sign in.",
    ]
    exhausted = set(exhausted)
    for user in policy.effective_users():
        if user.admin:
            continue
        if user.uid in exhausted:
            lines.append(f"*;*;{user.username};!Al0000-2400")
            continue
        times = pam_times(grid(user.time))
        if times:
            lines.append(f"*;*;{user.username};{times}")
    return "\n".join(lines) + "\n"


def write_time_conf(policy, exhausted=(), path: Path = TIME_CONF) -> None:
    content = render_time_conf(policy, exhausted)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(tmp, 0o644)
    os.rename(tmp, path)


# -- the day's budget --------------------------------------------------------

def day_start(now: float) -> int:
    t = time.localtime(now)
    return int(time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 0, 0, 0, 0, 0, -1)))


class UsageStore:
    """Seconds of active use per account per day, kept across reboots.

    One JSON line per tick — {"t": when, "uid": who, "s": seconds} — appended
    with O_APPEND, the same shape as the activity log, and summed for the
    current local day on read. Kept in memory between ticks so the file is
    read once at start, and trimmed to two days because yesterday is only
    needed until midnight has safely passed.
    """

    def __init__(self, path: Path = USAGE_PATH):
        self.path = Path(path)
        self._loaded = False
        self._entries: list[tuple[int, int, int]] = []   # (t, uid, seconds)

    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        try:
            lines = self.path.read_bytes().splitlines()
        except OSError:
            return
        for raw in lines:
            try:
                doc = json.loads(raw)
                self._entries.append((int(doc["t"]), int(doc["uid"]), int(doc["s"])))
            except (ValueError, KeyError, TypeError):
                continue

    def add(self, uid: int, seconds: int, now: float) -> None:
        """Note `seconds` more of active use; never raises."""
        if seconds <= 0:
            return
        self._load()
        entry = (int(now), int(uid), int(seconds))
        self._entries.append(entry)
        line = json.dumps({"t": entry[0], "uid": entry[1], "s": entry[2]},
                          separators=(",", ":"))
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            try:
                os.write(fd, (line + "\n").encode())
            finally:
                os.close(fd)
        except OSError as e:
            log.warning("could not record time use in %s: %s", self.path, e)

    def used_today(self, uid: int, now: float) -> int:
        self._load()
        since = day_start(now)
        return sum(s for t, u, s in self._entries if u == uid and t >= since)

    def trim(self, now: float) -> None:
        """Drop what is older than two days and rewrite the file."""
        self._load()
        cutoff = int(now) - USAGE_KEEP_SECONDS
        kept = [e for e in self._entries if e[0] >= cutoff]
        if len(kept) == len(self._entries):
            return
        self._entries = kept
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            tmp = self.path.with_suffix(".tmp")
            with open(tmp, "w") as handle:
                for t, u, s in kept:
                    handle.write(json.dumps({"t": t, "uid": u, "s": s},
                                            separators=(",", ":")) + "\n")
            os.chmod(tmp, 0o600)
            os.rename(tmp, self.path)
        except OSError as e:
            log.warning("could not trim %s: %s", self.path, e)


# -- a sign-in pam_time refused ----------------------------------------------

def record_refused_login(environ=None, spool: Path | None = None,
                         getpwnam=None) -> bool:
    """Note in the activity log that pam_time turned a sign-in away.

    Run by pam_exec from the account stack, after pam_time has said no,
    with PAM_USER and PAM_SERVICE in the environment. Returns whether a
    line was written.
    """
    import pwd

    from . import activity

    environ = os.environ if environ is None else environ
    getpwnam = pwd.getpwnam if getpwnam is None else getpwnam
    username = environ.get("PAM_USER", "")
    if not username:
        return False
    try:
        uid = getpwnam(username).pw_uid
    except KeyError:
        return False
    return activity.record("pam", activity.TIME, uid, why=WHY_LOGIN,
                           service=environ.get("PAM_SERVICE", ""), spool=spool)


def refused_login_main() -> int:
    """Entry point for /usr/bin/kosher-time-refused (see os-image)."""
    return 0 if record_refused_login() else 1
