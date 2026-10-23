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
import re
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


# -- what is newest, judged against what is running (#112) ------------------
#
# `bootc upgrade --check` answers "No changes" for any image whose contents
# the machine already holds, and after a rollback the version the family
# went back from is exactly that: still on the disk, as the deployment to
# go forward to, and the newest on the channel. The Updates page then said
# the running version was the newest, while a black-screen 0.13.0 sat in
# the registry. So the registry is asked for the channel's current digest,
# "newer" means newer than the BOOTED deployment, and the answer also says
# where a newest-already-downloaded version sits: staged (restart to use
# it) or rollback (the one you went back from).

INSPECT_TIMEOUT = 60


def inspect_registry(image: str, run=subprocess.run) -> dict | None:
    """The registry's current digest and version for `image`, from skopeo
    (which reads the manifest and its labels; nothing is pulled). None when
    the registry cannot be asked, and bootc's own answer stands."""
    if not image:
        return None
    try:
        res = run(["skopeo", "inspect", "--no-tags", "--format",
                   '{{.Digest}} {{index .Labels "org.opencontainers.image.version"}}',
                   f"docker://{image}"], capture_output=True, text=True, timeout=INSPECT_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return None
    if res.returncode != 0:
        return None
    parts = (res.stdout or "").split()
    if not parts or not parts[0].startswith("sha256:"):
        return None
    return {"digest": parts[0], "version": parts[1] if len(parts) > 1 else None}


def judge_check(returncode: int, stdout: str, stderr: str, deployment: dict,
                registry: dict | None) -> dict:
    """The CheckUpdate answer from bootc's check and the registry's (#207).

    A check that could not reach the registry is not "up to date". bootc's
    error text has no "Update available" in it, so parse_check reads it as
    available=False, and with no network the registry lookup that would
    correct it fails too. When bootc failed and the registry did not
    answer, `available` is None (unknown) and `error` carries bootc's first
    line, so the pages say they could not check instead of "up to date".
    When bootc failed but the registry answered, the registry's digest
    decides, as it does after a rollback.
    """
    info = parse_check(stdout or stderr)
    info["ok"] = returncode == 0
    if not info["ok"] and registry is None:
        info["available"] = None
        info["error"] = next((line.strip() for line in (stderr or stdout or "").splitlines()
                              if line.strip()), "the update check failed")
        booted = deployment.get("booted") or {}
        info["booted_version"] = booted.get("version")
        info["on_disk"] = None
        return info
    out = reconcile(info, deployment, registry)
    if not info["ok"]:
        # The registry answered for bootc: the result is real.
        out["ok"] = True
    return out


def reconcile(info: dict, deployment: dict, registry: dict | None) -> dict:
    """bootc's answer (parse_check), corrected by the deployments' digests
    and the registry's: `available` is "newer than what is running",
    `on_disk` is "staged" or "rollback" when that newest version is already
    on the computer, `booted_version` is what is running."""
    booted = deployment.get("booted") or {}
    out = dict(info)
    out["booted_version"] = booted.get("version")
    out["on_disk"] = None
    newest = registry or {}
    digest = newest.get("digest") or info.get("digest")
    if not digest:
        return out
    out["digest"] = digest
    if newest.get("version"):
        out["version"] = newest["version"]
    if booted.get("digest") and booted["digest"] == digest:
        out["available"] = False
        return out
    out["available"] = True
    for place in ("staged", "rollback"):
        if (deployment.get(place) or {}).get("digest") == digest:
            out["on_disk"] = place
    return out


# -- image trust (#70) -------------------------------------------------------
#
# /etc/containers/policy.json holds an image from KosherOS's own repository
# to the KosherOS key's signature. bootc checks that policy only for a
# deployment that says so: the installer records the image as unverified
# (every machine installed from the ISO starts that way, and so does one
# bootc installed for a VM), and `bootc upgrade` keeps whatever the
# deployment says. `bootc switch --enforce-container-sigpolicy` onto the
# very same image is what turns it on — the same pull and the same
# restart as an update, so it is done as the machine's first update after
# the policy arrives, and every update after it is verified.

POLICY_PATH = "/etc/containers/policy.json"


def policy_requires_signature(image: str, policy_path: str = POLICY_PATH) -> bool:
    """Does the image policy demand a signature for this image?

    The most specific `docker` scope wins, as containers-policy.json(5)
    has it: the repository, then each parent namespace, then the registry
    (with its port), then the transport's "" entry, then the global
    default. A policy that cannot be read requires nothing: bootc would
    refuse the pull on its own, with its own words.
    """
    try:
        with open(policy_path, encoding="utf-8") as f:
            policy = json.load(f)
    except (OSError, ValueError):
        return False
    docker = (policy.get("transports") or {}).get("docker") or {}
    repo = repo_of(image)
    parts = repo.split("/")
    scopes = ["/".join(parts[:i]) for i in range(len(parts), 0, -1)] + [""]
    rules = next((docker[s] for s in scopes if s in docker), None)
    if rules is None:
        rules = policy.get("default") or []
    return any(isinstance(r, dict) and r.get("type") in ("sigstoreSigned", "signedBy")
               for r in rules)


def upgrade_argv(booted_image: str | None, verified: bool,
                 policy_path: str = POLICY_PATH) -> tuple[str, ...]:
    """The command for "update this machine": `bootc upgrade`, or, once,
    the switch that turns verification on (see above)."""
    if booted_image and not verified and policy_requires_signature(booted_image, policy_path):
        return ("bootc", "switch", "--enforce-container-sigpolicy", booted_image)
    return ("bootc", "upgrade")


def switch_argv(target: str, policy_path: str = POLICY_PATH) -> tuple[str, ...]:
    """The command for moving onto `target`: verified where the policy
    holds the target to a signature, plain where it does not (a developer's
    own build on a local registry)."""
    if policy_requires_signature(target, policy_path):
        return ("bootc", "switch", "--enforce-container-sigpolicy", target)
    return ("bootc", "switch", target)


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
    return run_upgrade(on_progress, on_finished, argv=switch_argv(image))


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


# -- trying a build that is not on a channel (#90) ---------------------------
#
# The maintainer's laptop runs edge. To try the image CI built for a pull
# request, the machine switches onto that build's tag, and on the next try
# it would lose the edge it came from: bootc keeps the deployment it is
# booted into and the new one, and drops the rest. So the first try PINS
# the deployment that was running (`ostree admin pin`), which keeps it
# through however many builds are tried after it; each try after the first
# removes the test builds that are no longer booted, so the disk holds the
# kept image, the running test build and nothing older; and "done" rolls
# back onto the kept deployment and unpins it. Nothing here is for
# families: the admin app does not show it, and kosherctl needs the update
# right and the guardian password, as a channel switch does.

PR_TAG = "pr-"
TRIAL_TAG = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]{0,127}$")


def trial_target(image: str, what: str) -> str:
    """The image reference to try, from what a person typed: `123` or
    `pr-123` is a pull request's build, anything else is a tag on this
    machine's own repository, and a full reference is accepted only when
    it names that repository (as switch_target has it)."""
    repo = repo_of(image)
    if not repo:
        raise ValueError("this machine does not say which image it runs, "
                         "so there is nothing to try a build of")
    what = (what or "").strip()
    if not what:
        raise ValueError("say which build: a pull request number, or a tag")
    if what.isdigit():
        return f"{repo}:{PR_TAG}{int(what)}"
    if "/" in what:
        if repo_of(what) != repo:
            raise ValueError(f"{what} is not a build of this computer's image "
                             f"({repo}); only those can be tried")
        return what
    if what.lower() in CHANNEL_NAMES:
        raise ValueError(f"{what} is a channel: follow it with "
                         f"'kosherctl system channel {what}'")
    if not TRIAL_TAG.match(what):
        raise ValueError(f"{what!r} is not a tag")
    return f"{repo}:{what}"


def deployments(status: dict) -> list[dict]:
    """Every deployment in `bootc status --json`'s `status`, in one list:
    staged, booted, rollback, then the others bootc lists, each with its
    `place`, image reference, channel, digest, whether it is pinned, and
    the `key` (`checksum.serial`) ostree names it by."""
    out = []
    for place in ("staged", "booted", "rollback"):
        entry = status.get(place)
        if isinstance(entry, dict):
            out.append(_deployment(place, entry))
    for entry in status.get("otherDeployments") or []:
        if isinstance(entry, dict):
            out.append(_deployment("other", entry))
    return out


def _deployment(place: str, entry: dict) -> dict:
    outer = entry.get("image") if isinstance(entry.get("image"), dict) else {}
    inner = outer.get("image") if isinstance(outer.get("image"), dict) else {}
    ref = inner.get("image")
    tree = entry.get("ostree") if isinstance(entry.get("ostree"), dict) else {}
    key = None
    if tree.get("checksum") is not None:
        key = f"{tree.get('checksum')}.{tree.get('deploySerial', 0)}"
    # `channel` is a real channel or nothing: a pr- or version tag is not one.
    channel = _channel(ref) if ref else None
    return {"place": place, "image": ref,
            "channel": channel if channel in CHANNEL_NAMES else None,
            "digest": outer.get("imageDigest"), "pinned": bool(entry.get("pinned")),
            "verified": inner.get("signature") == "containerPolicy", "key": key}


OSTREE_DEPLOYMENT = re.compile(
    r"^(?P<mark>[* ]) (?P<stateroot>\S+) (?P<checksum>[0-9a-f]{64})\.(?P<serial>\d+)"
    r"(?P<flags>(?: \([^)]*\))*)\s*$")


def parse_ostree_status(text: str) -> list[dict]:
    """`ostree admin status` -> its deployments in index order, which is
    the index `ostree admin pin` and `undeploy` take. Each is {index, key,
    booted, pinned, staged}; the indented lines under a deployment (its
    version and origin) are not needed here."""
    out = []
    for line in (text or "").splitlines():
        m = OSTREE_DEPLOYMENT.match(line)
        if not m:
            continue
        flags = m.group("flags") or ""
        out.append({"index": len(out),
                    "key": f"{m.group('checksum')}.{int(m.group('serial'))}",
                    "booted": m.group("mark") == "*",
                    "pinned": "(pinned)" in flags,
                    "staged": "(staged)" in flags})
    return out


def _stale_trials(deps: list[dict], keep_staged: bool) -> list[dict]:
    """Test builds no longer needed: not booted, not pinned, not on a
    channel (a channel deployment is left to ostree's own housekeeping)."""
    return [d for d in deps
            if d["place"] != "booted" and not d["pinned"] and not d["channel"]
            and (d["place"] != "staged" or not keep_staged)]


def trial_plan(deps: list[dict], target: str, policy_path: str = POLICY_PATH) -> dict:
    """What trying `target` takes, from the deployments: `pin` (the key of
    the booted deployment, when nothing is kept yet), `undeploy` (keys of
    stale test builds), `argv` (the bootc command), and `kept`.

    A try of the build already booted or already staged is an upgrade,
    not a switch: `bootc switch` onto the reference in use is a no-op,
    and `bootc upgrade` is what fetches that reference's newest digest,
    which after a new push to the pull request is the point.
    """
    booted = next((d for d in deps if d["place"] == "booted"), None)
    if booted is None or not booted.get("image"):
        raise ValueError("this machine does not say which image it runs, "
                         "so there is nothing to keep while trying another")
    kept = next((d for d in deps if d["pinned"]), None)
    pin = None
    if kept is None:
        if not booted.get("key"):
            raise ValueError("bootc did not say which deployment is booted, "
                             "so it cannot be kept")
        pin, kept = booted["key"], booted
    staged = next((d for d in deps if d["place"] == "staged"), None)
    if target == booted.get("image"):
        argv = upgrade_argv(booted["image"], bool(booted.get("verified")), policy_path)
    elif staged is not None and target == staged.get("image"):
        argv = ("bootc", "upgrade")
    else:
        argv = switch_argv(target, policy_path)
    undeploy = [d["key"] for d in _stale_trials(deps, keep_staged=True)
                if d["key"] and d is not kept]
    return {"pin": pin, "undeploy": undeploy, "argv": argv, "kept": kept}


def return_plan(deps: list[dict]) -> dict:
    """What going back to the kept deployment takes: `kept`, `undeploy`
    (stale test builds, so the kept one is next in line), `rollback`
    (whether `bootc rollback` is needed, i.e. the kept one is not what is
    booted), and `unpin` (its key: trying is over)."""
    kept = next((d for d in deps if d["pinned"]), None)
    if kept is None:
        raise ValueError("nothing is kept: this computer is not trying a build")
    booted = next((d for d in deps if d["place"] == "booted"), None)
    back = kept is not booted
    # A staged test build is discarded by bootc rollback itself; when the
    # kept deployment is the booted one there is no rollback to do that,
    # so the staged build is undeployed along with the rest.
    undeploy = [d["key"] for d in _stale_trials(deps, keep_staged=back)
                if d["key"] and d is not kept]
    return {"kept": kept, "undeploy": undeploy, "rollback": back, "unpin": kept["key"]}


# -- automatic updates (#19) -------------------------------------------------
#
# Like a phone: the computer looks for a new version on a schedule the
# administrator picks, downloads it in the background, and leaves it
# staged for the next restart. It never restarts on its own. The people
# signed in get one notice per version (when the administrator has not
# turned that off), and a small mark in the panel for as long as the
# update waits.
#
# A systemd timer (kosher-auto-update.timer) fires once a day, at a random
# time that stays the same for each machine, and runs
# `kosherctl updates run`; kosherd decides here whether a check is due.
# A systemd calendar cannot say "every two weeks", and a daily tick also
# means a check that failed (no network that day) is tried again the next
# day rather than a whole period later. Persistent=true makes a machine
# that was off at the time catch up when it starts. "off" stops the timer
# altogether, and the run refuses as well.

SCHEDULE_DAYS = {"daily": 1, "weekly": 7, "biweekly": 14, "monthly": 30}
SCHEDULE_LABELS = {
    "off": "Off",
    "daily": "Daily",
    "weekly": "Weekly",
    "biweekly": "Every 2 weeks",
    "monthly": "Monthly",
}
# The daily tick wanders by up to the timer's random delay, so a weekly
# check six days and twenty hours after the last one counts as a week.
DUE_SLACK = 6 * 3600
AUTO_TIMER = "kosher-auto-update.timer"
AUTO_STATE_PATH = "/var/lib/kosher/auto-update.json"
# Below this, on battery, a download of a few gigabytes waits for the
# charger. Running flat half-way breaks nothing, but nothing gets done
# either, and a flat battery is worse than a late update.
LOW_BATTERY = 40
BOOT_ID_PATH = "/proc/sys/kernel/random/boot_id"

READY_SUMMARY = "An update is ready"
READY_BODY = "It'll be used the next time this computer restarts."


def next_due(schedule: str, last_success: float | None) -> float | None:
    """When the next automatic check is due, as a timestamp: None when the
    schedule is off, 0 when no check has worked yet (due now)."""
    days = SCHEDULE_DAYS.get(schedule)
    if days is None:
        return None
    if not last_success:
        return 0.0
    return float(last_success) + days * 86400 - DUE_SLACK


def is_metered(value) -> bool:
    """NetworkManager's Metered property (NMMetered): 1 is yes, 3 is a
    guessed yes. Unknown (0) and anything unreadable count as not metered:
    a machine that cannot say is far more often on home broadband than on
    a phone's hotspot."""
    return value in (1, 3)


def battery_too_low(on_battery, percent) -> bool:
    if not on_battery:
        return False
    try:
        return float(percent) < LOW_BATTERY
    except (TypeError, ValueError):
        return False


def skip_reason(schedule: str, state: dict, now: float, *, force: bool = False,
                metered: bool = False, on_battery: bool = False,
                battery=None, busy: bool = False) -> str | None:
    """Why an automatic run should not check now, in a few words for the
    journal, or None to go ahead. `force` (kosherctl updates run --now)
    skips only "not due yet": off is off, and the network and battery
    rules stand."""
    if schedule not in SCHEDULE_DAYS:
        return "automatic updates are off"
    if not force:
        due = next_due(schedule, (state or {}).get("last_success"))
        if due and now < due:
            return "not due yet"
    if busy:
        return "an update is already downloading"
    if metered:
        return "the network is metered"
    if battery_too_low(on_battery, battery):
        return f"the battery is below {LOW_BATTERY}%"
    return None


def after_check(info: dict) -> str:
    """What an automatic run does with a CheckUpdate-shaped answer:
    "none" (up to date), "download", "staged" (already downloaded), or
    "left": the newest is the version this machine went back from, which
    somebody chose to leave, so it is not fetched again unasked."""
    if not info.get("available"):
        return "none"
    on_disk = info.get("on_disk")
    if on_disk == "staged":
        return "staged"
    if on_disk == "rollback":
        return "left"
    return "download"


def check_failure(returncode: int, stdout: str, stderr: str) -> str | None:
    """bootc's check failed: its first line, for the journal and the admin
    app, or None when it worked. A failed check is never "up to date"
    (#207): bootc's error has no "Update available" in it, so read the
    ordinary way it looks like no update."""
    if returncode == 0:
        return None
    return next((line.strip() for line in (stderr or stdout or "").splitlines()
                 if line.strip()), "the update check failed")


def staged_to_discard(before: list[dict], after: list[dict]) -> str | None:
    """After a failed download: the key of a staged deployment that was not
    there before it, so the failure leaves the machine as it found it.
    bootc stages only at the end of a pull that worked, so this is almost
    always None; it is here for the time it is not."""
    old = next((d for d in before if d["place"] == "staged"), None)
    new = next((d for d in after if d["place"] == "staged"), None)
    if new is None or not new.get("key"):
        return None
    if old is not None and old.get("key") == new["key"]:
        return None
    return new["key"]


def load_state(path: str | None = None) -> dict:
    # Looked up now, not at import, so a test can point it elsewhere.
    path = path or AUTO_STATE_PATH
    try:
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, ValueError):
        return {}
    return doc if isinstance(doc, dict) else {}


def save_state(state: dict, path: str | None = None) -> None:
    path = path or AUTO_STATE_PATH
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
        f.write("\n")
    os.replace(tmp, path)


def boot_id(path: str | None = None) -> str:
    path = path or BOOT_ID_PATH
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def record_staged(state: dict, version: str | None, digest: str | None, boot: str,
                  *, announce: bool) -> bool:
    """Note the update waiting for a restart in `state`. Returns whether the
    people signed in get the pop-up: only when `announce` (an automatic
    download, with notices on) and only the first time this version is
    seen, so a daily check that keeps finding the same download says so
    once."""
    state["staged_version"] = version or ""
    state["staged_digest"] = digest or ""
    state["staged_boot"] = boot
    if not announce or not digest or state.get("announced_digest") == digest:
        return False
    state["announced_digest"] = digest
    return True


def clear_staged(state: dict) -> bool:
    """Nothing waits for a restart any more. Returns whether something did."""
    had = bool(state.get("staged_digest"))
    for key in ("staged_version", "staged_digest", "staged_boot"):
        state.pop(key, None)
    return had


# libostree writes this while a deployment is staged for the next boot and
# finalizes it at shutdown; it is gone once nothing is staged.
STAGED_MARK = "/run/ostree/staged-deployment"


def staged_on_disk(path: str | None = None) -> bool:
    return os.path.exists(path or STAGED_MARK)


def pending_update(state: dict, boot: str) -> dict | None:
    """The update downloaded and waiting: {"version", "digest"}, or None
    once the computer has restarted (the update is in use then) or when
    nothing waits. Read from kosherd's own note, so any account may ask
    without bootc being run for it."""
    digest = state.get("staged_digest")
    if not digest or not boot or state.get("staged_boot") != boot:
        return None
    return {"version": state.get("staged_version") or "", "digest": digest}
