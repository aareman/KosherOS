"""How much each person used the internet, and how much the filter did.

The admin app's Usage page: pages and files loaded, data in and out,
pages blocked, pictures hidden, videos and searches refused, and how long
the filter took over each request — for today, the past week, the past
month and all time, per account and for everyone.

Only numbers are kept. No address, no search, no page title: the
activity log (activity.py) already says what was blocked for the week it
covers, and this is not allowed to become a browsing history by the back
door.

The storage has to stay small however long the computer runs, so nothing
here is a log of requests:

- The proxy and the search service keep running totals in memory (Tally)
  and append them to their own file in the activity spool once a minute,
  one line per account per day. A writer stops appending when its file
  reaches MAX_SPOOL_BYTES and keeps adding up in memory instead, so a
  kosherd that is not folding cannot fill the disk.
- kosherd folds those lines into /var/lib/kosher/usage.json and empties
  the spool (fold). That file holds one entry per account per day for
  KEEP_DAYS days, and a running total per account for all time. Days that
  age out have already been added to the total, so nothing is lost and
  nothing grows.

Filter time is kept as a histogram with fixed bounds rather than a list
of samples, so a median and a 95th percentile can be read for any range
by adding days together, at a fixed cost per day.
"""

from __future__ import annotations

import datetime
import json
import logging
import os
import threading
import time
from pathlib import Path

from .activity import SPOOL_DIR

log = logging.getLogger(__name__)

STORE_PATH = Path("/var/lib/kosher/usage.json")
SUFFIX = ".usage"
FOLDING = ".folding"

# What is counted. Anything else on a line is ignored.
COUNTERS = ("requests", "bytes_in", "bytes_out", "blocked", "pictures",
            "videos", "searches")

# Upper bounds of the filter-time buckets, in milliseconds. The last
# bucket is everything slower than the last bound.
LATENCY_BOUNDS_MS = (1, 2, 5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000)
BUCKETS = len(LATENCY_BOUNDS_MS) + 1

# A month on the page is thirty days; sixty leaves room for the page to
# grow a "previous month" without a migration. 60 days x a family's
# accounts x a few hundred bytes is well under a megabyte.
KEEP_DAYS = 60
FLUSH_SECONDS = 60
MAX_SPOOL_BYTES = 4 * 1024 * 1024
# Bounds on what one line may claim, so a corrupt or hostile line cannot
# poison the totals.
MAX_COUNT = 10 ** 15
MAX_LINE = 2000

RANGES = {"today": 1, "week": 7, "month": 30}


def day_key(when: float | None = None) -> str:
    """The family's local date. A day on the page is the family's day."""
    return datetime.date.fromtimestamp(
        when if when is not None else time.time()).isoformat()


def bucket_of(ms: float) -> int:
    for i, bound in enumerate(LATENCY_BOUNDS_MS):
        if ms <= bound:
            return i
    return BUCKETS - 1


def empty() -> dict:
    entry = {name: 0 for name in COUNTERS}
    entry["lat"] = [0] * BUCKETS
    entry["lat_ms"] = 0
    return entry


def merge(into: dict, other: dict) -> dict:
    """Add `other`'s numbers to `into`, ignoring anything malformed."""
    for name in (*COUNTERS, "lat_ms"):
        value = other.get(name)
        if isinstance(value, int) and not isinstance(value, bool) \
                and 0 <= value <= MAX_COUNT:
            into[name] = min(into.get(name, 0) + value, MAX_COUNT)
    lat = other.get("lat")
    if isinstance(lat, list) and len(lat) == BUCKETS:
        into.setdefault("lat", [0] * BUCKETS)
        for i, value in enumerate(lat):
            if isinstance(value, int) and not isinstance(value, bool) \
                    and 0 <= value <= MAX_COUNT:
                into["lat"][i] = min(into["lat"][i] + value, MAX_COUNT)
    return into


# -- the writers' side ---------------------------------------------------------


class Tally:
    """Running totals in a writer's memory, flushed to its spool file.

    Keyed by (day, uid) so a minute that spans midnight lands in the right
    day. Thread-safe: the search service answers on threads.
    """

    def __init__(self, writer: str, spool: Path | None = None,
                 every: float = FLUSH_SECONDS):
        self.path = (Path(spool) if spool is not None else SPOOL_DIR) / \
            f"{_safe_name(writer)}{SUFFIX}"
        self.every = every
        self.pending: dict[tuple[str, int], dict] = {}
        self.flushed_at = time.monotonic()
        self.lock = threading.Lock()

    def add(self, uid: int | None, **counts: int) -> None:
        if uid is None:
            return
        with self.lock:
            entry = self._entry(int(uid))
            for name, value in counts.items():
                if name in COUNTERS and value:
                    entry[name] += int(value)

    def latency(self, uid: int | None, seconds: float) -> None:
        """One request's time in the filter."""
        if uid is None or seconds < 0:
            return
        ms = seconds * 1000
        with self.lock:
            entry = self._entry(int(uid))
            entry["lat"][bucket_of(ms)] += 1
            entry["lat_ms"] += int(round(ms))

    def _entry(self, uid: int) -> dict:
        key = (day_key(), uid)
        entry = self.pending.get(key)
        if entry is None:
            entry = self.pending[key] = empty()
        return entry

    def due(self) -> bool:
        return time.monotonic() - self.flushed_at >= self.every

    def flush(self) -> bool:
        """Append what has been counted. Never raises.

        Kept in memory when the file is full or cannot be written, and
        tried again next time; memory is bounded by accounts x days.
        """
        with self.lock:
            self.flushed_at = time.monotonic()
            if not self.pending:
                return True
            pending, self.pending = self.pending, {}
        lines = []
        for (day, uid), entry in pending.items():
            document = {"day": day, "uid": uid}
            document.update({k: v for k, v in entry.items()
                             if k != "lat" and v})
            if any(entry["lat"]):
                document["lat"] = entry["lat"]
            lines.append(json.dumps(document, separators=(",", ":")))
        data = ("\n".join(lines) + "\n").encode()
        try:
            fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
            try:
                if os.fstat(fd).st_size + len(data) > MAX_SPOOL_BYTES:
                    raise OSError("the usage spool is full; kosherd is not folding it")
                os.write(fd, data)
            finally:
                os.close(fd)
        except OSError as e:
            log.warning("could not write usage to %s: %s", self.path, e)
            with self.lock:
                for key, entry in pending.items():
                    merge(self.pending.setdefault(key, empty()), entry)
            return False
        return True


def _safe_name(writer: str) -> str:
    return "".join(c for c in writer if c.isalnum() or c in "-_") or "unknown"


# -- kosherd's side -------------------------------------------------------------


def load(path: Path | None = None) -> dict:
    path = Path(path) if path is not None else STORE_PATH
    try:
        store = json.loads(path.read_text())
    except (OSError, ValueError):
        store = {}
    if not isinstance(store, dict):
        store = {}
    for key in ("days", "total"):
        if not isinstance(store.get(key), dict):
            store[key] = {}
    return store


def save(store: dict, path: Path | None = None) -> None:
    path = Path(path) if path is not None else STORE_PATH
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w") as handle:
        json.dump(store, handle, separators=(",", ":"))
    os.chmod(tmp, 0o600)
    os.rename(tmp, path)


def fold(*, spool: Path | None = None, path: Path | None = None,
         now: float | None = None) -> int:
    """Move every writer's lines into the store. Root only.

    Each spool file is renamed before it is read, so a writer that flushes
    meanwhile starts a new file rather than losing its lines to the
    truncate. A file left half-folded by a crash is picked up next time.
    Returns how many lines were folded.
    """
    spool = Path(spool) if spool is not None else SPOOL_DIR
    try:
        names = [p for p in spool.iterdir() if p.name.endswith(SUFFIX)]
    except OSError:
        return 0
    taken = []
    for source in names:
        target = source.with_name(source.name + FOLDING)
        if target.exists():
            continue  # an earlier fold's leftover goes first, below
        try:
            os.rename(source, target)
        except OSError as e:
            log.warning("could not take %s: %s", source, e)
    try:
        taken = [p for p in spool.iterdir() if p.name.endswith(SUFFIX + FOLDING)]
    except OSError:
        return 0
    if not taken:
        _prune_and_maybe_save(None, path, now)
        return 0

    store = load(path)
    folded = 0
    for source in taken:
        try:
            with open(source, "rb") as handle:
                lines = handle.read().splitlines()
        except OSError:
            continue
        for raw in lines:
            if len(raw) > MAX_LINE:
                continue
            try:
                document = json.loads(raw)
            except ValueError:
                continue
            if not _valid_line(document):
                continue
            uid = str(document["uid"])
            day = store["days"].setdefault(document["day"], {})
            merge(day.setdefault(uid, empty()), document)
            merge(store["total"].setdefault(uid, empty()), document)
            store.setdefault("since", document["day"])
            store["since"] = min(store["since"], document["day"])
            folded += 1
    _prune_and_maybe_save(store, path, now)
    for source in taken:
        try:
            source.unlink()
        except OSError as e:
            log.warning("could not clear %s: %s", source, e)
    return folded


def _valid_line(document) -> bool:
    if not isinstance(document, dict):
        return False
    uid = document.get("uid")
    day = document.get("day")
    if not isinstance(uid, int) or isinstance(uid, bool) or uid < 0:
        return False
    try:
        datetime.date.fromisoformat(day)
    except (TypeError, ValueError):
        return False
    return True


def _prune_and_maybe_save(store: dict | None, path, now) -> None:
    """Drop days older than KEEP_DAYS. Their numbers are in the totals."""
    loaded = store is None
    if loaded:
        store = load(path)
    oldest = (datetime.date.fromtimestamp(now if now is not None else time.time())
              - datetime.timedelta(days=KEEP_DAYS - 1)).isoformat()
    stale = [day for day in store["days"] if day < oldest]
    for day in stale:
        del store["days"][day]
    if stale or not loaded:
        try:
            save(store, path)
        except OSError as e:
            log.warning("could not save usage: %s", e)


def summarise(entry: dict) -> dict:
    """The page's numbers for one account over one range."""
    out = {name: entry.get(name, 0) for name in COUNTERS}
    lat = entry.get("lat") or [0] * BUCKETS
    count = sum(lat)
    out["timed"] = count
    out["avg_ms"] = round(entry.get("lat_ms", 0) / count, 1) if count else None
    out["p50_ms"] = _percentile(lat, 0.50)
    out["p95_ms"] = _percentile(lat, 0.95)
    return out


def _percentile(lat: list[int], fraction: float) -> int | None:
    """The bucket bound the given share of requests finished within. For
    the overflow bucket that is "more than the last bound", returned as
    the bound itself; the page says "over" for it."""
    total = sum(lat)
    if not total:
        return None
    seen = 0
    for i, value in enumerate(lat):
        seen += value
        if seen >= fraction * total:
            return LATENCY_BOUNDS_MS[min(i, len(LATENCY_BOUNDS_MS) - 1)]
    return LATENCY_BOUNDS_MS[-1]


def report(store: dict, now: float | None = None) -> dict:
    """{range: {"everyone": numbers, "people": {uid: numbers}}} for today,
    the past week, the past month and all time."""
    today = datetime.date.fromtimestamp(now if now is not None else time.time())
    out = {}
    for name, days in RANGES.items():
        wanted = {(today - datetime.timedelta(days=i)).isoformat()
                  for i in range(days)}
        people: dict[str, dict] = {}
        for day, entries in store.get("days", {}).items():
            if day not in wanted or not isinstance(entries, dict):
                continue
            for uid, entry in entries.items():
                if isinstance(entry, dict):
                    merge(people.setdefault(uid, empty()), entry)
        out[name] = _ranged(people)
    out["all"] = _ranged({uid: entry for uid, entry in store.get("total", {}).items()
                          if isinstance(entry, dict)})
    out["since"] = store.get("since")
    return out


def _ranged(people: dict[str, dict]) -> dict:
    everyone = empty()
    for entry in people.values():
        merge(everyone, entry)
    return {"everyone": summarise(everyone),
            "people": {uid: summarise(entry) for uid, entry in people.items()}}
