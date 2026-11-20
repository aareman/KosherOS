"""When this administrator last looked at Activity, and what is new since.

Kept per account, in the account's own state folder, not in kosherd's
policy: two administrators each have their own "last looked", and nothing
here changes what the filter does. A missing or unreadable file means
"never looked", so a first visit shows today's items as new.

What counts as new is what the feed shows as a line of its own (repeats
folded, as on the page), newer than the last look. An administrator's own
setting changes are not news to them and are never counted.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from kosherd import activity as activity_mod

FILE_NAME = "activity-seen"


def state_dir() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or os.path.join(Path.home(), ".local", "state")
    return Path(base) / "kosheros-admin"


def load() -> int:
    """When Activity was last looked at, or 0 for never."""
    try:
        value = json.loads((state_dir() / FILE_NAME).read_text()).get("seen", 0)
    except (OSError, ValueError, AttributeError):
        return 0
    return int(value) if isinstance(value, (int, float)) and value > 0 else 0


def save(when: int | None = None) -> int:
    """Note that Activity was looked at now (or at `when`). A file that
    cannot be written costs a badge that does not clear, never an error."""
    when = int(time.time()) if when is None else int(when)
    folder = state_dir()
    try:
        folder.mkdir(parents=True, exist_ok=True)
        tmp = folder / (FILE_NAME + ".tmp")
        tmp.write_text(json.dumps({"seen": when}))
        os.replace(tmp, folder / FILE_NAME)
    except OSError:
        pass
    return when


def is_news(event: dict) -> bool:
    return event.get("kind") != activity_mod.CHANGE


def unseen_today(events: list[dict], seen: int, now: float | None = None) -> int:
    """How many of today's feed lines are newer than `seen`."""
    from .feed import fold

    start = activity_mod.day_start(now)
    return sum(1 for event in fold(events)
               if is_news(event) and event["t"] >= start and event["t"] > seen)


def since(seen: int, now: float | None = None) -> int:
    """Where to start reading activity to count today's unseen lines."""
    return max(activity_mod.day_start(now), seen + 1 if seen else 0)
