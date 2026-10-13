"""Rootless containers, under the account's own filter (issue #34).

Podman is the one container engine on KosherOS, rootless, and `docker`
is Podman's own shim. A rootless container's network is a user-space
process (pasta) owned by the account, so what leaves the container
carries the account's uid and the firewall dispatches it like anything
else that account opens. Docker's daemon would not be: its bridge
traffic leaves through the kernel's forward path with no socket owner
to dispatch on, which is why there is none.

What a container needs that the account did not have before is a block
of subordinate ids: an image carries files owned by many uids, and
without a range to map them to Podman cannot unpack it (every
devcontainer, for a start). Fedora's useradd hands each new account a
block of 65536; the accounts made before the image allowed that are
given one here. A process inside the container that is not root there
runs on the host as one of these ids — with `--network=host` its sockets
carry that id, not the account's — so nft.py maps the account's block to
the account's own chain: the container is filtered as its owner,
whichever id it is running as.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)

SUBUID = Path("/etc/subuid")
SUBGID = Path("/etc/subgid")
# The block each account gets, and where the blocks start: Fedora's
# login.defs (SUB_UID_MIN, SUB_UID_COUNT), which useradd follows.
BLOCK = 65536
FIRST = 524288
LAST = 600100000

Ranges = dict[str, list[tuple[int, int]]]


def parse(text: str) -> Ranges:
    """/etc/subuid: `owner:start:count` per line, owner a name or a uid.
    Returns {owner: [(start, end), ...]} with inclusive ends."""
    found: Ranges = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(":")
        if len(parts) != 3:
            continue
        try:
            start, count = int(parts[1]), int(parts[2])
        except ValueError:
            continue
        if count > 0:
            found.setdefault(parts[0], []).append((start, start + count - 1))
    return found


def read(path: Path | None = None) -> Ranges:
    try:
        return parse((path or SUBUID).read_text())
    except OSError:
        return {}


def of_users(users, ranges: Ranges) -> dict[int, list[tuple[int, int]]]:
    """Each account's blocks, by uid, for the firewall."""
    out: dict[int, list[tuple[int, int]]] = {}
    for user in users:
        blocks = ranges.get(user.username, []) + ranges.get(str(user.uid), [])
        if blocks:
            out[user.uid] = sorted(set(blocks))
    return out


def next_free(ranges: Ranges, block: int = BLOCK) -> int | None:
    """The lowest start at or above FIRST where a block fits."""
    taken = sorted(r for blocks in ranges.values() for r in blocks)
    start = FIRST
    for s, e in taken:
        if e < start:
            continue
        if s > start + block - 1:
            break
        start = e + 1
    return start if start + block - 1 <= LAST else None


def ensure(users, run=subprocess.run) -> dict[int, list[tuple[int, int]]]:
    """Give every managed account a block it lacks, and return every
    account's blocks by uid. Never raises: a machine where this cannot
    be done is filtered exactly as before, with no block to map."""
    ranges = read()
    for user in sorted(users, key=lambda u: u.uid):
        if user.uid < 1000 or user.username in ranges or str(user.uid) in ranges:
            continue
        start = next_free(ranges)
        if start is None:
            log.warning("no subordinate id block left for %s", user.username)
            break
        end = start + BLOCK - 1
        res = run(["usermod", "--add-subuids", f"{start}-{end}",
                   "--add-subgids", f"{start}-{end}", user.username],
                  capture_output=True, text=True)
        if res.returncode != 0:
            log.warning("could not give %s subordinate ids: %s", user.username,
                        (res.stderr or "").strip())
            continue
        ranges.setdefault(user.username, []).append((start, end))
        log.info("gave %s subordinate ids %d-%d (rootless containers)", user.username, start, end)
    return of_users(users, ranges)
