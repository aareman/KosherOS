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


# -- channels ---------------------------------------------------------------
#
# Two of them, and they are tags on the same image rather than different
# images, so moving between them is `bootc switch` onto the other tag and
# nothing else: no reinstall, no second registry, and the files and
# settings on the machine are untouched. The wording is aimed at a parent
# deciding for the family computer, not at somebody who knows what a
# container tag is.

CHANNELS: tuple[dict, ...] = (
    {
        "name": "stable",
        "title": "Stable",
        "summary": "updates that have been tested first",
        "description": "Updates arrive here after they have been tested, so "
                       "they come less often and are less likely to break "
                       "something. Stay on this one unless you are helping "
                       "test KosherOS.",
    },
    {
        "name": "edge",
        "title": "Edge",
        "summary": "every new version as soon as it is built",
        "description": "Every new version arrives here as soon as it is "
                       "built, often several in a week. Things break here "
                       "sometimes; that is what it is for.",
        # Shown when confirming the switch. Only the direction that costs
        # something has one: a warning on every choice is a warning
        # nobody reads.
        "caution": "Edge versions have not been tested, and things break "
                   "there sometimes.",
    },
)
CHANNEL_NAMES: tuple[str, ...] = tuple(c["name"] for c in CHANNELS)
DEFAULT_CHANNEL = "stable"


def repo_of(image: str) -> str:
    """An image reference without its tag or digest.

    'ghcr.io/aareman/kosher-linux:edge' -> 'ghcr.io/aareman/kosher-linux'.
    Only the last path segment is looked at, because the registry part may
    carry a port ('localhost:5000/x:edge') whose colon is not a tag.
    """
    ref = (image or "").split("@", 1)[0].strip()
    head, sep, tail = ref.rpartition("/")
    name = tail.split(":", 1)[0]
    return f"{head}{sep}{name}" if sep else name


def switch_target(image: str, channel: str) -> str:
    """The image reference for `channel`, keeping the repository `image`
    came from.

    A machine's own image is the starting point rather than a constant, so
    a build from somewhere else (a fork, a local registry, a test
    machine) switches within its own repository instead of being quietly
    moved onto ghcr.io. Switching also works from a pinned version tag,
    which is how a machine parked on one release rejoins a channel.
    """
    if channel not in CHANNEL_NAMES:
        raise ValueError(f"unknown channel: {channel}")
    repo = repo_of(image)
    if not repo:
        raise ValueError("this machine does not say which image it runs, "
                         "so there is nothing to switch from")
    return f"{repo}:{channel}"


def describe_channels(image: str | None, staged: str | None = None) -> dict:
    """What the admin app and kosherctl show: the channels, which one this
    machine is on, and what it would switch to.

    `current` is None when the machine runs something that is not a
    channel at all — a pinned version, a digest, or a locally built image
    — which is a real state (it is what every machine installed before
    channels existed is in) and reads as "not on a channel" rather than as
    an error.

    `pending` is the point of `staged`. A switch does not change the
    running deployment, only the one queued for the next restart, so right
    after switching the booted image still names the old channel. Without
    this the screen would say the switch had not happened.
    """
    image = image or ""
    current = _channel(image)
    if current not in CHANNEL_NAMES:
        current = None
    queued = _channel(staged or "")
    if queued not in CHANNEL_NAMES or queued == current:
        queued = None
    out = []
    for entry in CHANNELS:
        row = dict(entry)
        row["current"] = entry["name"] == current
        row["pending"] = entry["name"] == queued
        try:
            row["image"] = switch_target(image, entry["name"])
        except ValueError:
            row["image"] = None
        out.append(row)
    return {"current": current, "pending": queued, "image": image or None,
            "default": DEFAULT_CHANNEL, "channels": out}


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

    thread = threading.Thread(target=worker, name="-".join(argv[:2]), daemon=True)
    thread.start()
    return thread


def run_switch(image: str, on_progress: Progress,
               on_finished: Finished) -> threading.Thread:
    """Move this machine onto another image reference — the same pull, the
    same progress and the same "takes effect at the next restart" as an
    update, because that is exactly what it is."""
    return run_upgrade(on_progress, on_finished, argv=("bootc", "switch", image))


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
        what = " ".join(a for a in argv[:2] if not a.startswith("-"))
        return False, f"{what or 'bootc'} failed: {tail}".strip()
    on_progress(100, "Update ready")
    return True, ""
