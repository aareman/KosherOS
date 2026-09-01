"""Asking for a page, and an admin answering.

Every filter is wrong sometimes. What decides whether a family keeps using
one is not how often it is wrong, it is what happens next. If the answer
is "find the parent, get them to open a settings app, have them work out
which of five settings caused it and type the address in by hand", the
real answer is that the filter gets turned off.

So the block page has a button. It records who asked, for what, and why,
and the admin app shows the queue with one button to allow it. The request
carries no authority of its own — nothing changes until an admin says so —
which is what lets the asking be frictionless.

Requests are dropped into a spool directory as individual files rather
than appended to one queue, because the two processes that can create them
(the filtering proxy and the search service) run as different unprivileged
users, and a file each needs no locking and no shared writer. The
directory is write-only to them: they may drop a request, they may not
read anyone else's.
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from pathlib import Path

log = logging.getLogger(__name__)

# Its own directory rather than one under /var/lib/kosher, which is 0700:
# the two services that drop requests here are unprivileged and cannot
# even traverse into the daemon's state. This one is 0730 root:kosher-spool
# — those services may create a request, and may not read anyone else's.
SPOOL_DIR = Path("/var/lib/kosher-requests")

MAX_URL = 2000
MAX_NOTE = 500
# A queue nobody empties should not be able to fill the disk.
MAX_PENDING = 200


class RequestError(ValueError):
    pass


def _spool(spool) -> Path:
    # Resolved per call, not bound as a default at import: SPOOL_DIR is a
    # module setting, and a default argument would freeze whatever it was
    # when the module first loaded.
    return Path(spool) if spool is not None else SPOOL_DIR


def submit(uid: int, url: str, note: str = "", *,
           spool: Path | None = None) -> str:
    """Record a request for access. Returns its id.

    Called by the proxy and the search service, which are unprivileged and
    can do nothing else about it.
    """
    url = (url or "").strip()
    if not url:
        raise RequestError("no address")
    if len(url) > MAX_URL:
        raise RequestError("address too long")
    if not url.startswith(("http://", "https://")):
        raise RequestError("only web addresses can be requested")

    spool = _spool(spool)
    request_id = uuid.uuid4().hex
    document = {
        "id": request_id,
        "uid": int(uid),
        "url": url,
        "note": (note or "").strip()[:MAX_NOTE],
        "asked": int(time.time()),
    }
    path = spool / f"{request_id}.json"
    tmp = spool / f".{request_id}.tmp"
    try:
        # Written then renamed: a reader must never see half a request.
        with open(tmp, "w") as handle:
            json.dump(document, handle)
        os.chmod(tmp, 0o644)
        os.rename(tmp, path)
    except OSError as e:
        # The spool is created by tmpfiles.d at boot. If it is not there,
        # say so in words the person can act on rather than showing them a
        # traceback — asking is the one thing that must not feel broken.
        log.error("cannot write to the request spool %s: %s", spool, e)
        raise RequestError(
            "this computer could not record the request") from None
    return request_id


def pending(*, spool: Path | None = None) -> list[dict]:
    """Every request waiting for an answer, oldest first."""
    spool = _spool(spool)
    found = []
    try:
        names = sorted(spool.iterdir())
    except OSError:
        return []
    for path in names:
        if path.suffix != ".json":
            continue
        try:
            document = json.loads(path.read_text())
        except (OSError, ValueError):
            # A request we cannot read is a request nobody can answer.
            log.warning("discarding an unreadable access request: %s", path.name)
            _unlink(path)
            continue
        if not _valid(document):
            _unlink(path)
            continue
        found.append(document)
    found.sort(key=lambda d: d["asked"])
    if len(found) > MAX_PENDING:
        # Oldest first, so what is dropped is what has been ignored longest.
        for document in found[:-MAX_PENDING]:
            _unlink(spool / f"{document['id']}.json")
        found = found[-MAX_PENDING:]
    return found


def resolve(request_id: str, *, spool: Path | None = None) -> dict | None:
    """Take one request off the queue and return it."""
    if not _is_id(request_id):
        return None
    path = _spool(spool) / f"{request_id}.json"
    try:
        document = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    _unlink(path)
    return document if _valid(document) else None


def _valid(document) -> bool:
    return (isinstance(document, dict)
            and _is_id(str(document.get("id", "")))
            and isinstance(document.get("uid"), int)
            and isinstance(document.get("url"), str)
            and document["url"].startswith(("http://", "https://"))
            and len(document["url"]) <= MAX_URL)


def _is_id(value: str) -> bool:
    # Ids come back from a UI and are turned into paths; anything that is
    # not a plain hex id has no business being one.
    return len(value) == 32 and all(c in "0123456789abcdef" for c in value)


def _unlink(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass
