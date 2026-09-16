"""Applying an OS update while saying how far along it is.

`bootc upgrade` pulls an image that is gigabytes on a slow line, and a
button that goes dead for ten minutes is a button that gets pressed
again. bootc can report progress on a file descriptor as JSON lines; this
runs it that way in a thread, turns each line into (percent, words), and
hands them to whoever is listening — the daemon, which turns them into
D-Bus signals for the admin app.

The parsing is a pure function, tolerant by design: bootc's progress
schema is newer than its command line and has changed once already, so
every field is optional and an unreadable line is a status with no
percentage rather than a crash mid-update.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
from typing import Callable

Progress = Callable[[int, str], None]      # percent (-1 when unknown), status
Finished = Callable[[bool, str], None]     # ok, error


def parse_progress(line: str) -> tuple[int, str] | None:
    """One JSON line from bootc's progress fd -> (percent, status), or None
    for a line that says nothing a person would want to read.

    Bytes beat steps when both are present: "1.2 GB of 3 GB" moves
    smoothly, "step 2 of 4" jumps. Either is reported against the whole
    task, not the current layer, so the bar never runs backwards.
    """
    try:
        doc = json.loads(line)
    except (ValueError, TypeError):
        return None
    if not isinstance(doc, dict):
        return None
    kind = doc.get("type", "")
    if not str(kind).startswith("Progress"):
        return None
    words = doc.get("description") or doc.get("task") or "Updating"
    percent = -1
    total = doc.get("bytesTotal")
    done = doc.get("bytes")
    if isinstance(total, (int, float)) and total > 0 and isinstance(done, (int, float)):
        percent = int(min(100, max(0, done * 100 // total)))
    else:
        total = doc.get("stepsTotal")
        done = doc.get("steps")
        if isinstance(total, (int, float)) and total > 0 and isinstance(done, (int, float)):
            percent = int(min(100, max(0, done * 100 // total)))
    # A subtask says which layer; a parent does not care which, but "Pulling
    # layer 3 of 7" is more reassuring than the same word for ten minutes.
    steps, steps_total = doc.get("steps"), doc.get("stepsTotal")
    if isinstance(steps, int) and isinstance(steps_total, int) and steps_total > 1:
        words = f"{words} ({min(steps + 1, steps_total)} of {steps_total})"
    return percent, str(words)


def parse_check(text: str) -> dict:
    """`bootc upgrade --check` output -> what a person wants from it.

    bootc prints, for example:

        Update available for: ghcr.io/aareman/kosher-linux:edge
          Version: 0.1.0-pre.055
          Digest: sha256:…

    or `No changes in: ghcr.io/…:edge`. The page used to show the first
    line, which names the image and not what you would get; the version
    is the thing to see at a glance. Every field is optional, because
    bootc's wording has moved before and will again; `raw` keeps the
    original for the case none of it matched.
    """
    import re

    text = text or ""
    version = re.search(r"Version:\s*(\S+)", text)
    digest = re.search(r"Digest:\s*(\S+)", text)
    image = re.search(r"(?:Update available for|No changes in|No update available for|"
                      r"Already at latest):?\s*(\S+)", text)
    lowered = text.lower()
    available = "update available" in lowered or ("version:" in lowered
                                                 and "no changes" not in lowered)
    return {
        "available": bool(available),
        "version": version.group(1) if version else None,
        "digest": digest.group(1) if digest else None,
        "image": image.group(1) if image else None,
        "channel": _channel(image.group(1)) if image else None,
        "raw": text.strip(),
    }


def _channel(image: str) -> str | None:
    """'ghcr.io/x/kosher-linux:edge' -> 'edge'; a digest reference has none."""
    if not image or "@" in image:
        return None
    tail = image.rsplit("/", 1)[-1]
    return tail.split(":", 1)[1] if ":" in tail else None


def run_upgrade(on_progress: Progress, on_finished: Finished,
                argv: tuple[str, ...] = ("bootc", "upgrade")) -> threading.Thread:
    """Start the upgrade in a thread and return it.

    Progress arrives on a pipe bootc is told about with --progress-fd; the
    command's own output is kept for the error message. A bootc too old to
    know --progress-fd fails at once, and the plain command is run instead
    with no percentage — a slow update is still better than no update.
    """

    def worker():
        ok, error = _run(list(argv), on_progress, with_progress=True)
        if not ok and "progress-fd" in error:
            on_progress(-1, "Updating…")
            ok, error = _run(list(argv), on_progress, with_progress=False)
        on_finished(ok, error)

    thread = threading.Thread(target=worker, name="bootc-upgrade", daemon=True)
    thread.start()
    return thread


def _run(argv: list[str], on_progress: Progress, with_progress: bool) -> tuple[bool, str]:
    read_fd = write_fd = None
    if with_progress:
        read_fd, write_fd = os.pipe()
        argv = argv + ["--progress-fd", str(write_fd)]
    try:
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, pass_fds=(write_fd,) if with_progress else ())
    except OSError as e:
        if write_fd is not None:
            os.close(write_fd)
            os.close(read_fd)
        return False, f"could not start {argv[0]}: {e}"
    if with_progress:
        os.close(write_fd)  # only bootc holds the write end now
        with os.fdopen(read_fd, "r", errors="replace") as progress:
            for line in progress:
                parsed = parse_progress(line)
                if parsed is not None:
                    on_progress(*parsed)
    output = proc.communicate()[0] or ""
    if proc.returncode != 0:
        tail = "\n".join(output.strip().splitlines()[-3:])
        return False, f"bootc upgrade failed: {tail}".strip()
    on_progress(100, "Update ready")
    return True, ""
