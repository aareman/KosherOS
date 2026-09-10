"""What the filter did, written down so a parent can see it.

A filter that blocks a page and says nothing to anyone but the child is
half a product. The other half is the parent being able to open the admin
app and see "Yosef, 12:41, youtube.com, video and streaming" — and turn
that block into an allow with one click, before anyone has to ask.

This is a record of the FILTER, not of the person. Only what the filter
did goes in: pages it blocked, pictures it hid, searches it refused, and
the settings changes admins made. What was allowed through is never
written, so the log cannot become a browsing history.

Writers are the filtering proxy and the search service, both unprivileged,
plus kosherd itself. Each appends JSON lines to ITS OWN file in a directory
that is 0730 root:kosher-spool, the same arrangement as the request spool:
a writer may create and append to its file and cannot read anyone else's.
kosherd, as root, is the only reader. One line per event and O_APPEND, so
concurrent writes from one process never interleave. Everything is
trimmed to a week: the point is what happened today, not an archive.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

log = logging.getLogger(__name__)

SPOOL_DIR = Path("/var/lib/kosher-activity")

# What can be recorded. Anything else is dropped on read.
BLOCK = "block"          # a page refused, with why
PICTURES = "pictures"    # pictures hidden on a page (once per page, per window)
VIDEO = "video"          # a video refused after being looked at
SEARCH = "search"        # a search that would not run
CHANGE = "change"        # an admin changed a setting
KINDS = frozenset({BLOCK, PICTURES, VIDEO, SEARCH, CHANGE})

# How the log says why a page was blocked. Kept machine-readable so the
# admin app can turn "category:video" into "Video and streaming" in the
# family's own language, and so an allow can be offered where one would
# help (a category block, yes; a "reads as explicit" block, also yes; a
# page rule the admin wrote, less so).
WHY_CATEGORY = "category"   # category:video,social
WHY_RULE = "rule"           # rule:<pattern>
WHY_CONTENT = "content"     # content:nsfw
WHY_LANGUAGE = "language"   # language
WHY_SHOP = "shop"           # shop:<department>
WHY_YOUTUBE = "youtube"     # youtube:channel | youtube:category

KEEP_SECONDS = 7 * 24 * 3600
MAX_LINE = 4000
MAX_URL = 2000
# Reading back stops here: a feed nobody will scroll past a few hundred of
# has no business loading tens of thousands of lines into memory.
MAX_EVENTS = 5000


def _spool(spool) -> Path:
    return Path(spool) if spool is not None else SPOOL_DIR


def record(writer: str, kind: str, uid: int | None, *, url: str = "",
           why: str = "", spool: Path | None = None, **extra) -> bool:
    """Append one event. Never raises: a failed note is not a failed filter."""
    if kind not in KINDS:
        raise ValueError(f"unknown activity kind {kind!r}")
    document = {"t": int(time.time()), "kind": kind,
                "uid": int(uid) if uid is not None else -1}
    if url:
        document["url"] = str(url)[:MAX_URL]
    if why:
        document["why"] = str(why)[:300]
    for key, value in extra.items():
        if value is not None and value != "" and value != []:
            document[key] = value
    line = json.dumps(document, separators=(",", ":"))
    if len(line) > MAX_LINE:
        document.pop("args", None)
        line = json.dumps(document, separators=(",", ":"))[:MAX_LINE]
    path = _spool(spool) / f"{_safe_name(writer)}.jsonl"
    try:
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        try:
            os.write(fd, (line + "\n").encode())
        finally:
            os.close(fd)
    except OSError as e:
        log.warning("could not record activity in %s: %s", path, e)
        return False
    return True


def _safe_name(writer: str) -> str:
    return "".join(c for c in writer if c.isalnum() or c in "-_") or "unknown"


def events(since: int = 0, uid: int | None = None, *,
           spool: Path | None = None) -> list[dict]:
    """Everything recorded since `since`, newest first.

    `uid` narrows to one account; None is everyone. Malformed lines are
    skipped rather than fatal — a truncated line at a crash is a line, not
    a reason the whole feed fails to load.
    """
    spool = _spool(spool)
    try:
        paths = [p for p in spool.iterdir() if p.suffix == ".jsonl"]
    except OSError:
        return []
    found: list[dict] = []
    for path in paths:
        try:
            with open(path, "rb") as handle:
                lines = handle.read().splitlines()
        except OSError:
            continue
        for raw in lines:
            try:
                document = json.loads(raw)
            except ValueError:
                continue
            if not _valid(document) or document["t"] < since:
                continue
            if uid is not None and document["uid"] != uid:
                continue
            found.append(document)
    found.sort(key=lambda d: d["t"], reverse=True)
    return found[:MAX_EVENTS]


def _valid(document) -> bool:
    return (isinstance(document, dict)
            and document.get("kind") in KINDS
            and isinstance(document.get("t"), int)
            and isinstance(document.get("uid"), int))


def summary(found: list[dict]) -> dict[int, dict]:
    """Per account: how many pages were blocked, pictures hidden, and when
    the filter last did anything. For the cards on the family board."""
    out: dict[int, dict] = {}
    for document in found:
        entry = out.setdefault(document["uid"],
                               {"blocked": 0, "pictures": 0, "searches": 0,
                                "last": 0})
        if document["kind"] in (BLOCK, VIDEO):
            entry["blocked"] += 1
        elif document["kind"] == PICTURES:
            entry["pictures"] += 1
        elif document["kind"] == SEARCH:
            entry["searches"] += 1
        else:
            continue
        entry["last"] = max(entry["last"], document["t"])
    return out


def trim(*, spool: Path | None = None, now: int | None = None) -> None:
    """Drop everything older than a week. Root only: rewrites the files.

    Lines that do not parse go too — they will never be shown, and keeping
    them only makes the next read slower.
    """
    spool = _spool(spool)
    cutoff = (now if now is not None else int(time.time())) - KEEP_SECONDS
    try:
        paths = [p for p in spool.iterdir() if p.suffix == ".jsonl"]
    except OSError:
        return
    for path in paths:
        try:
            with open(path, "rb") as handle:
                lines = handle.read().splitlines()
        except OSError:
            continue
        kept = []
        for raw in lines:
            try:
                document = json.loads(raw)
            except ValueError:
                continue
            if _valid(document) and document["t"] >= cutoff:
                kept.append(raw)
        if len(kept) == len(lines):
            continue
        try:
            stat = path.stat()
            tmp = path.with_suffix(".tmp")
            with open(tmp, "wb") as handle:
                handle.write(b"\n".join(kept) + (b"\n" if kept else b""))
            os.chown(tmp, stat.st_uid, stat.st_gid)
            os.chmod(tmp, 0o644)
            os.rename(tmp, path)
        except OSError as e:
            log.warning("could not trim %s: %s", path, e)


def day_start(now: float | None = None) -> int:
    """Local midnight before `now`, as a timestamp. 'Today' on the board
    means the family's today, not UTC's."""
    t = time.localtime(now if now is not None else time.time())
    return int(time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 0, 0, 0,
                            0, 0, -1)))
