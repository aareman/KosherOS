#!/usr/bin/env python3
"""Test a pull request, a branch or this checkout in a real VM, without sudo.

    scripts/pr-test.py [TARGET] [--full | --all] [--direct] [--vms N] [--keep] [--gl]

TARGET is a pull request number, a branch on origin, or HEAD (the default:
this checkout as it is, which is what CI hands over). Anything other than
HEAD is checked out into a temporary worktree, so the checkout you are
working in is left alone.

What runs is decided by the paths the change touches (pr_test_select.py),
and the report says why. The stages:

  build    `just build` in the target's tree
  base     once per master revision: master's image installed on a fresh
           disk and set up by the boot test, kept in ~/.cache/kosher-pr-test
           (KOSHER_PR_TEST_CACHE), shared by every checkout of the repo
  switch   that machine `bootc switch`ed to the change's image from a
           local registry, the way an update reaches a family (or from a
           public registry CI pushed it to: --registry-image)
  disk, boot
           instead, for a change to power-on and setup (or --full, or in
           CI): a fresh disk from the change's own image and
           scripts/boot-test.py on it
  desktop  a test account signs in through GDM; its session, the polkit
           answers, the password prompts, the apps, the filter in Firefox
  suites-N the target's scripts/vm-tests, split over several machines

The desktop and the suite groups run at the same time, each on its own
overlay of the set-up machine.

Everything lands in build/pr-test/<ID>/: summary.json (for machines),
report.md (for people), each stage's log, the serial consoles and the
screenshots. A failure listed in scripts/vm-tests/known-failures.txt is
known; the exit status is non-zero when anything else failed.

CI (vm-test.yml) runs the same stages over several runners: one builds
the image and pushes it to ghcr, and each of the others restores master's
base from the Actions cache (made by ci.yml with --base-only), switches it
to that image (--registry-image) and runs one --shard. A runner without a
usable base takes the direct path (--prefer-base).

Nothing here runs sudo. A `sudo` that fails loudly is put first on the PATH
of every command the harness starts, and the just recipes it calls are
checked beforehand (scripts/check-no-sudo.py).
"""

from __future__ import annotations

import argparse
import atexit
import base64
import contextlib
import fcntl
import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# The account boot-test.py makes in the wizard: the computer's administrator.
ADMIN_USER = "boottest"
ADMIN_PASSWORD = "boot-test-pw-1"
# The account that signs in at the login screen. Filtered, not an
# administrator, so the polkit rules have something to refuse.
USER = "prkid"
USER_PASSWORD = "pr-test-pw-1"

BLOCKED_URL = "https://www.unibet.com/"  # gambling: in the default floor
ALLOWED_URL = "https://example.com/"


# --- running things -----------------------------------------------------------

def sudo_trap(workdir: Path) -> dict:
    """An environment whose `sudo` fails and says who called it."""
    trap = workdir / "no-sudo"
    trap.mkdir(parents=True, exist_ok=True)
    script = trap / "sudo"
    script.write_text(
        "#!/bin/sh\n"
        f"echo \"pr-test: something ran sudo: $*\" >> {shlex.quote(str(workdir / 'sudo-attempts.log'))}\n"
        "echo 'pr-test: sudo is not allowed in the VM harness' >&2\n"
        "exit 97\n")
    script.chmod(0o755)
    env = dict(os.environ)
    env["PATH"] = f"{trap}:{env.get('PATH', '')}"
    env["SUDO_ASKPASS"] = "/bin/false"
    return env


def run_logged(cmd: list[str], log: Path, env: dict, cwd: Path | None = None,
               timeout: int | None = None) -> int:
    """Run a command with its output going to a log and to this terminal."""
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a") as fh:
        fh.write(f"$ {shlex.join(cmd)}\n")
        fh.flush()
        proc = LEFTOVERS.start(cmd, cwd=cwd, env=env, stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, text=True, errors="replace", bufsize=1)
        try:
            deadline = time.monotonic() + timeout if timeout else None
            assert proc.stdout is not None
            for line in proc.stdout:
                fh.write(line)
                sys.stdout.write("    " + line)
                if deadline and time.monotonic() > deadline:
                    LEFTOVERS.stop(proc)
                    fh.write(f"pr-test: timed out after {timeout}s\n")
                    break
            return proc.wait()
        finally:
            LEFTOVERS.stop(proc)


# --- what a run leaves behind -----------------------------------------------------

# bcvk makes its 40 GiB sparse install disk as /var/tmp/.tmpXXXXXX (the one
# host directory its container sees) and removes it only when it exits
# cleanly. A run that is killed, or a bcvk that crashes, leaves it there.
BCVK_TMP = Path("/var/tmp")
BCVK_TMP_NAME = re.compile(r"^\.tmp[A-Za-z0-9]{6}$")


def bcvk_temps(root: Path = BCVK_TMP) -> set[tuple[str, int]]:
    """The temp disks in ROOT that look like bcvk's and are ours: (name, inode)."""
    found = set()
    try:
        entries = list(os.scandir(root))
    except OSError:
        return found
    for e in entries:
        if not BCVK_TMP_NAME.match(e.name):
            continue
        try:
            st = e.stat(follow_symlinks=False)
        except OSError:
            continue
        if stat.S_ISREG(st.st_mode) and st.st_uid == os.getuid():
            found.add((e.name, st.st_ino))
    return found


def open_files() -> set[str]:
    """Every path a process we can see holds open."""
    held = set()
    for fd_dir in Path("/proc").glob("[0-9]*/fd"):
        try:
            fds = list(fd_dir.iterdir())
        except OSError:
            continue
        for fd in fds:
            try:
                held.add(os.readlink(fd))
            except OSError:
                pass
    return held


class Leftovers:
    """What a run makes that must not outlive it, and the processes it runs.

    The run's machines and disks, its image tag, and any temp disk bcvk
    made while the run was making a disk are recorded here as they are
    made, and clean() removes them however the run ends: at the end of
    main(), on SIGTERM or SIGINT, or at exit. clean() runs once.
    """

    def __init__(self, tmp_root: Path = BCVK_TMP):
        self.tmp_root = tmp_root
        self.paths: list[Path] = []
        self.images: list[str] = []      # build tags, folded into CACHE_TAG
        self.worktrees: list[Path] = []
        self.windows: list[dict] = []    # times a disk was being made
        self.label = f"kosher.pr-test.run={os.getpid()}-{int(time.time())}"
        self.keep = False
        self.done = False
        self._procs: set = set()
        self._lock = threading.RLock()  # the signal handler may take it again

    # the processes a run starts, each in its own process group so that
    # stopping it stops what it started too (bcvk, podman, QEMU)
    def start(self, cmd: list[str], **kw) -> subprocess.Popen:
        proc = subprocess.Popen(cmd, start_new_session=True, **kw)
        with self._lock:
            self._procs.add(proc)
        return proc

    def stop(self, proc: subprocess.Popen, grace: float = 10) -> None:
        if proc.poll() is None:
            for sig, wait in ((signal.SIGTERM, grace), (signal.SIGKILL, 5)):
                with contextlib.suppress(OSError):
                    os.killpg(proc.pid, sig)
                try:
                    proc.wait(timeout=wait)
                    break
                except subprocess.TimeoutExpired:
                    continue
        with self._lock:
            self._procs.discard(proc)

    def stop_all(self) -> None:
        with self._lock:
            procs = list(self._procs)
        for p in procs:
            self.stop(p)

    def path(self, p: Path) -> Path:
        self.paths.append(p)
        return p

    def image(self, ref: str) -> str:
        if ref not in self.images:
            self.images.append(ref)
        return ref

    @contextlib.contextmanager
    def making_disk(self):
        """Around a run of rootless-disk.sh: every bcvk temp disk that
        appears meanwhile is this run's."""
        window = {"before": bcvk_temps(self.tmp_root), "after": None}
        self.windows.append(window)
        try:
            yield
        finally:
            window["after"] = bcvk_temps(self.tmp_root)

    def stray_temps(self) -> list[Path]:
        """bcvk temp disks that appeared while this run was making a disk
        and are still there."""
        now = None
        found: set[tuple[str, int]] = set()
        for w in self.windows:
            after = w["after"]
            if after is None:  # the run ended while a disk was being made
                now = now if now is not None else bcvk_temps(self.tmp_root)
                after = now
            found |= after - w["before"]
        here = bcvk_temps(self.tmp_root)
        return sorted(self.tmp_root / name for name, ino in found & here)

    def clean(self) -> list[str]:
        """Remove what the run made; what it removed, for the log."""
        if self.done:
            return []
        self.done = True
        self.stop_all()
        if self.keep:
            return []
        removed = []
        # bcvk's container, if bcvk was killed before it could stop it.
        ids = subprocess.run(["podman", "ps", "-aq", "--filter", f"label={self.label}"],
                             capture_output=True, text=True).stdout.split()
        if ids:
            subprocess.run(["podman", "rm", "-f", *ids], capture_output=True)
            removed += [f"container {i}" for i in ids]
        held = open_files()
        for temp in self.stray_temps():
            if str(temp) in held:  # someone else's bcvk, still at work
                continue
            temp.unlink(missing_ok=True)
            removed.append(str(temp))
        for p in self.paths:
            if p.is_dir() and not p.is_symlink():
                shutil.rmtree(p, ignore_errors=True)
                removed.append(str(p))
            elif p.exists() or p.is_symlink():
                p.unlink(missing_ok=True)
                removed.append(str(p))
        for tree in self.worktrees:
            subprocess.run(["git", "-C", str(REPO), "worktree", "remove", "--force", str(tree)],
                           capture_output=True)
        for ref in self.images:
            if image_id(ref):
                keep_build_cache(ref)
                removed.append(ref)
        return removed


LEFTOVERS = Leftovers()
# Set when the run is told to stop: the machines' threads stop waiting.
STOPPING = threading.Event()


def dangling_to_prune(images: list[dict], in_use: set[str], now: float,
                      max_age: float = 86400) -> list[str]:
    """Of the dangling images (each {"id", "added"}), the ones older than
    MAX_AGE seconds that no container, buildah's included, uses."""
    return [im["id"] for im in images
            if im["id"] not in in_use and im.get("added") is not None
            and now - im["added"] > max_age]


def prune_dangling(max_age: float = 86400) -> list[str]:
    """Remove dangling images a day old that nothing uses.

    An image's created time is no use for this: the images here are built
    with a fixed timestamp. When it was added to storage is the time its
    directory in container storage was written. An image with children is
    not dangling, so the layers under CACHE_TAG are never touched, and an
    image any container (or a build in progress) uses is left alone; rmi
    without --force refuses those as well.
    """
    ids = subprocess.run(["podman", "images", "-q", "--no-trunc", "--filter", "dangling=true"],
                         capture_output=True, text=True).stdout.split()
    if not ids:
        return []
    root = subprocess.run(["podman", "info", "--format", "{{.Store.GraphRoot}}"],
                          capture_output=True, text=True).stdout.strip()
    driver = subprocess.run(["podman", "info", "--format", "{{.Store.GraphDriverName}}"],
                            capture_output=True, text=True).stdout.strip() or "overlay"
    used = subprocess.run(["podman", "ps", "-a", "--external", "--no-trunc",
                           "--format", "{{.ImageID}}"],
                          capture_output=True, text=True).stdout.split()
    images = []
    for i in ids:
        i = i.removeprefix("sha256:")
        try:
            added = (Path(root) / f"{driver}-images" / i).stat().st_mtime
        except OSError:
            added = None
        images.append({"id": i, "added": added})
    gone = []
    for i in dangling_to_prune(images, {u.removeprefix("sha256:") for u in used}, time.time(),
                               max_age):
        if subprocess.run(["podman", "rmi", i], capture_output=True).returncode == 0:
            gone.append(i[:12])
    return gone


class Stage:
    def __init__(self, name: str, outdir: Path):
        self.name = name
        self.status = "skipped"
        self.seconds = 0.0
        self.checks: list[dict] = []
        self.log = outdir / f"{name}.log"
        self.note = ""
        self._t0 = 0.0

    def __enter__(self):
        print(f"\n=== {self.name} ===", flush=True)
        self._t0 = time.monotonic()
        self.status = "running"
        return self

    def __exit__(self, exc_type, exc, _tb):
        self.seconds = round(time.monotonic() - self._t0, 1)
        if exc is not None:
            self.status = "failed"
            self.note = self.note or f"{exc_type.__name__}: {exc}"
            print(f"  ERROR {self.note}")
        elif self.status == "running":
            self.status = "failed" if any(not c["ok"] for c in self.checks) else "passed"
        print(f"  [{self.name}: {self.status} in {self.seconds:.0f}s]", flush=True)
        # Any error ends this stage as failed and the run goes on to the
        # report; only an interrupt stops everything.
        return exc_type is not None and issubclass(exc_type, Exception)

    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        self.checks.append({"name": name, "ok": bool(ok), "detail": detail.strip()[-1500:]})
        print(f"  {'PASS' if ok else 'FAIL'}  [{self.name}] {name}", flush=True)
        if not ok and detail:
            print("        " + detail.strip()[-600:].replace("\n", "\n        "))
        return ok

    def as_dict(self) -> dict:
        return {"name": self.name, "status": self.status, "seconds": self.seconds,
                "checks": self.checks, "note": self.note, "log": self.log.name,
                "timings": getattr(self, "timings", None)}


class StageError(Exception):
    """Ends a stage as failed; the run goes on to the summary."""


# --- the machine ----------------------------------------------------------------

def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class QMP:
    """QEMU's control socket: keys, screenshots, the power button."""

    KEYS = {" ": "spc", "-": "minus", "_": ("shift", "minus"), ".": "dot",
            "/": "slash", "@": ("shift", "2"), "\n": "ret", "=": "equal",
            ":": ("shift", "semicolon"), ",": "comma"}

    def __init__(self, path: Path, timeout: int = 30):
        deadline = time.monotonic() + timeout
        while True:
            try:
                self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                self.sock.connect(str(path))
                break
            except (FileNotFoundError, ConnectionRefusedError):
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.5)
        self.file = self.sock.makefile("rw")
        self.file.readline()  # greeting
        self.execute("qmp_capabilities")

    def execute(self, command: str, **arguments) -> dict:
        msg = {"execute": command}
        if arguments:
            msg["arguments"] = arguments
        self.file.write(json.dumps(msg) + "\n")
        self.file.flush()
        while True:
            reply = json.loads(self.file.readline())
            if "event" in reply:
                continue
            if "error" in reply:
                raise RuntimeError(f"QMP {command}: {reply['error']}")
            return reply.get("return", {})

    def key(self, *qcodes: str) -> None:
        self.execute("send-key", keys=[{"type": "qcode", "data": q} for q in qcodes])
        time.sleep(0.08)

    def type(self, text: str) -> None:
        for ch in text:
            code = self.KEYS.get(ch)
            if code is None:
                if ch.isupper():
                    code = ("shift", ch.lower())
                elif ch.isalnum():
                    code = ch
                else:
                    raise ValueError(f"no key for {ch!r}")
            self.key(*(code if isinstance(code, tuple) else (code,)))

    def screenshot(self, path: Path) -> None:
        try:
            self.execute("screendump", filename=str(path), format="png")
        except RuntimeError:  # an older QEMU: PPM only
            self.execute("screendump", filename=str(path.with_suffix(".ppm")))

    def powerdown(self) -> None:
        self.execute("system_powerdown")


class Machine:
    """The test disk booted with a screen, ssh on a free port, and QMP."""

    def __init__(self, overlay: Path, outdir: Path, key: Path, gl: bool,
                 console: str = "desktop-console.log", mem: int = 4096):
        self.outdir = outdir
        outdir.mkdir(parents=True, exist_ok=True)
        self.key = key
        self.port = free_port()
        # Unix socket paths are limited to 108 bytes; the run directory is deeper.
        self.sockdir = LEFTOVERS.path(Path(tempfile.mkdtemp(prefix="kpt-")))
        self.qmp_path = self.sockdir / "qmp.sock"
        self.qmp_path.unlink(missing_ok=True)
        pub = key.with_suffix(".pub").read_text().strip()
        if gl:
            # The host's QEMU: the dev shell's cannot reach the host's GL.
            qemu = ["/usr/bin/qemu-system-x86_64", "-device", "virtio-vga-gl",
                    "-display", "egl-headless"]
        else:
            qemu = ["qemu-system-x86_64", "-device", "virtio-vga", "-display", "none"]
        cmd = qemu + [
            "-enable-kvm", "-cpu", "host", "-m", str(mem), "-smp", "4",
            "-drive", f"file={overlay},if=virtio",
            "-netdev", f"user,id=n0,hostfwd=tcp:127.0.0.1:{self.port}-:22",
            "-device", "virtio-net-pci,netdev=n0",
            "-qmp", f"unix:{self.qmp_path},server=on,wait=off",
            "-vnc", f"unix:{self.sockdir / 'vnc.sock'}",
            "-serial", f"file:{outdir / console}",
            # Root's key for this run, as a credential: the image's
            # tmpfiles write it to /root/.ssh/authorized_keys at boot.
            "-smbios", f"type=11,value=io.systemd.credential:ssh.authorized_keys.root={pub}",
        ]
        self.proc = LEFTOVERS.start(cmd, stdout=(outdir / "qemu.log").open("w"),
                                    stderr=subprocess.STDOUT)
        self.qmp = QMP(self.qmp_path)

    def ssh_cmd(self) -> list[str]:
        return ["ssh", "-i", str(self.key), "-p", str(self.port),
                "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
                "-o", "LogLevel=ERROR", "-o", "ConnectTimeout=5", "-o", "BatchMode=yes",
                "root@127.0.0.1"]

    def sh(self, script: str, timeout: int = 120, input: str | None = None) -> tuple[int, str]:
        """Run a shell script as root in the machine; (status, output)."""
        if STOPPING.is_set():
            raise StageError("the run was stopped")
        try:
            p = subprocess.run(self.ssh_cmd() + [script], input=input, text=True,
                               errors="replace", capture_output=True, timeout=timeout)
            return p.returncode, (p.stdout + p.stderr)
        except subprocess.TimeoutExpired as e:
            out = (e.stdout or b"").decode() if isinstance(e.stdout, bytes) else (e.stdout or "")
            return 124, out + f"\n(timed out after {timeout}s)"

    def wait_ssh(self, timeout: int = 420) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                return False
            if self.sh("true", timeout=15)[0] == 0:
                return True
            time.sleep(3)
        return False

    def as_user(self, user: str, command: str, timeout: int = 120) -> tuple[int, str]:
        """Run a command inside USER's graphical session's service manager.

        The process is a child of that account's systemd --user, so it has
        the session's Wayland display and session bus, and polkit counts
        it as the person signed in at the screen (it falls back to the
        account's display session for a process outside a session scope).
        """
        script = (
            f"uid=$(id -u {user}); "
            f"runuser -u {user} -- env XDG_RUNTIME_DIR=/run/user/$uid "
            f"DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/$uid/bus "
            f"systemd-run --user --quiet --wait --pipe --collect -- "
            f"bash -lc {shlex.quote(command)}")
        return self.sh(script, timeout=timeout)

    def start_as_user(self, user: str, unit: str, command: str) -> tuple[int, str]:
        script = (
            f"uid=$(id -u {user}); "
            f"runuser -u {user} -- env XDG_RUNTIME_DIR=/run/user/$uid "
            f"DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/$uid/bus "
            f"systemd-run --user --quiet --collect --unit={unit} -- "
            f"bash -lc {shlex.quote(command)}")
        return self.sh(script)

    def user_unit(self, user: str, verb: str, unit: str) -> tuple[int, str]:
        return self.sh(
            f"uid=$(id -u {user}); runuser -u {user} -- env XDG_RUNTIME_DIR=/run/user/$uid "
            f"DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/$uid/bus "
            f"systemctl --user {verb} {unit}")

    def shoot(self, name: str) -> Path:
        path = self.outdir / "screens" / f"{name}.png"
        path.parent.mkdir(exist_ok=True)
        try:
            self.qmp.screenshot(path)
        except (RuntimeError, OSError) as e:
            print(f"  (no screenshot {name}: {e})")
        return path

    def stop(self) -> None:
        try:
            self._stop()
        finally:
            LEFTOVERS.stop(self.proc)
            shutil.rmtree(self.sockdir, ignore_errors=True)

    def _stop(self) -> None:
        if self.proc.poll() is not None:
            return
        try:
            self.qmp.powerdown()
            self.proc.wait(timeout=120)
            return
        except (RuntimeError, OSError, subprocess.TimeoutExpired):
            pass
        self.proc.terminate()
        try:
            self.proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            self.proc.kill()


# --- the stages -----------------------------------------------------------------

def resolve_target(target: str, outdir: Path, env: dict, log: Path) -> tuple[Path, str]:
    """The tree to test and its commit."""
    if target in ("HEAD", "."):
        sha = subprocess.run(["git", "-C", str(REPO), "rev-parse", "HEAD"],
                             capture_output=True, text=True, check=True).stdout.strip()
        return REPO, sha
    ref = f"pull/{target}/head" if target.isdigit() else target
    if run_logged(["git", "-C", str(REPO), "fetch", "--quiet", "origin", ref], log, env) != 0:
        raise StageError(f"could not fetch {ref} from origin")
    sha = subprocess.run(["git", "-C", str(REPO), "rev-parse", "FETCH_HEAD"],
                         capture_output=True, text=True, check=True).stdout.strip()
    tree = outdir / "src"
    if tree.exists():
        subprocess.run(["git", "-C", str(REPO), "worktree", "remove", "--force", str(tree)],
                       capture_output=True)
    if run_logged(["git", "-C", str(REPO), "worktree", "add", "--detach", str(tree), sha],
                  log, env) != 0:
        raise StageError("could not check the target out")
    # The version is counted from tags; a fresh worktree shares them.
    return tree, sha


def stage_build(st: Stage, tree: Path, image: str, env: dict) -> None:
    e = dict(env, KOSHER_IMAGE=image)
    rc = run_logged(["just", "build"], st.log, e, cwd=tree, timeout=3600)
    st.check("the image builds", rc == 0, f"just build exited {rc}; see {st.log}")
    if rc != 0:
        raise StageError("build failed")


def stage_pull(st: Stage, registry: tuple[str, str], image: str, env: dict) -> None:
    """The registry image into local storage, under the run's tag, for the
    stages that install from an image (rootless-disk.sh) rather than
    switch to one."""
    name, digest = registry
    rc = run_logged(["podman", "pull", f"{name}@{digest}"], st.log, env, timeout=1800)
    ok = rc == 0 and subprocess.run(["podman", "tag", f"{name}@{digest}", image],
                                    capture_output=True).returncode == 0
    st.check(f"the image is pulled from {name}", ok, f"exit {rc}; see {st.log}")
    if not ok:
        raise StageError("no image")


def stage_disk(st: Stage, image: str, disk: Path, env: dict) -> None:
    # bcvk's container carries the run's label, so a run that is stopped
    # half way can remove it; its temp disk is found by when it appeared.
    e = dict(env, KOSHER_DISK_LABEL=LEFTOVERS.label)
    with LEFTOVERS.making_disk():
        rc = run_logged([str(REPO / "scripts/rootless-disk.sh"), image, str(disk)],
                        st.log, e, timeout=3600)
    ok = rc == 0 and disk.exists()
    st.check("a bootable disk is made without root", ok, f"exit {rc}; see {st.log}")
    if not ok:
        raise StageError("no disk")


def stage_boot(st: Stage, image: str, disk: Path, machine: Path, outdir: Path, env: dict) -> None:
    e = dict(env, KOSHER_IMAGE=image, PYTHONUNBUFFERED="1")
    rc = run_logged([sys.executable, str(REPO / "scripts/boot-test.py"), str(disk),
                     "--log", str(outdir / "boot-console.log"),
                     "--keep-overlay", str(machine)], st.log, e, timeout=1800)
    text = st.log.read_text()
    for m in re.finditer(r"^\s*(PASS|FAIL|SKIP)\s+(.*)$", text, re.M):
        if m.group(1) == "SKIP":
            st.checks.append({"name": m.group(2), "ok": True, "detail": "skipped"})
        else:
            st.checks.append({"name": m.group(2), "ok": m.group(1) == "PASS", "detail": ""})
    if rc != 0 and not any(not c["ok"] for c in st.checks):
        st.check("boot-test.py finished", False, f"exit {rc}")
    if not machine.exists() or not re.search(r"PASS\s+setup completes", text):
        machine.unlink(missing_ok=True)
        raise StageError("the wizard did not complete setup")


def stage_setup_over_ssh(st: Stage, disk: Path, machine: Path, outdir: Path, key: Path) -> None:
    """Set the machine up without the wizard, so the later stages still run.

    Only when the boot stage failed to get through setup. The boot stage
    stays failed and so does the run; this is so one broken wizard does
    not also hide what the desktop and the suites would have found. The
    administrator is the one the wizard would have made, through the
    non-interactive path of the same `kosherctl setup`.
    """
    subprocess.run(["qemu-img", "create", "-q", "-f", "qcow2", "-b", str(disk.resolve()),
                    "-F", "qcow2", str(machine)], check=True)
    vm = Machine(machine, outdir, key, gl=False, console="setup-console.log")
    try:
        if not st.check("the machine boots and answers ssh", vm.wait_ssh()):
            raise StageError("no ssh")
        rc, out = vm.sh(f"kosherctl setup --username {ADMIN_USER} --full-name 'Boot Test' "
                        "--password-stdin", input=ADMIN_PASSWORD + "\n", timeout=180)
        st.check("setup completes without the wizard (kosherctl setup --username)",
                 rc == 0, out)
        vm.shoot("00-setup-over-ssh")
    finally:
        vm.stop()
    if rc != 0:
        machine.unlink(missing_ok=True)
        raise StageError("setup over ssh failed too")


def prompt_on_screen(png: Path) -> bool:
    """Is GNOME's password dialog up? It is a navy box in the middle of the
    screen with white title text, which no app or login screen here has."""
    try:
        from PIL import Image
        im = Image.open(png).convert("RGB")
    except Exception:  # noqa: BLE001 - no screenshot, or no Pillow
        return False
    w, h = im.size

    def at(x: float, y: float) -> tuple:
        return im.getpixel((int(w * x), int(h * y)))

    navy = all(r < 35 and b < 75 and b - r > 25
               for r, _g, b in (at(0.5, 0.625), at(0.33, 0.5), at(0.67, 0.5)))
    title = min(at(0.5, 0.3)) > 200
    return navy and title


def wait_for(predicate, timeout: float, step: float = 2.0):
    deadline = time.monotonic() + timeout
    while True:
        value = predicate()
        if value or time.monotonic() > deadline:
            return value
        time.sleep(step)


def session_of(vm: Machine, user: str) -> dict:
    """The graphical session USER has on seat0, as loginctl describes it."""
    rc, out = vm.sh(
        f"for s in $(loginctl list-sessions --no-legend | awk '$3==\"{user}\" {{print $1}}'); do "
        "loginctl show-session $s -p Id -p Seat -p Type -p Class -p Active -p Remote -p State -p Leader; "
        "echo --; done")
    for block in out.split("--"):
        props = dict(line.split("=", 1) for line in block.strip().splitlines() if "=" in line)
        if props.get("Seat") == "seat0" and props.get("Class") == "user":
            return props
    return {}


KOSHERD_PING = ("busctl --system --timeout=10 call org.kosherlinux.Daemon1 "
                "/org/kosherlinux/Daemon1 org.freedesktop.DBus.Peer Ping")
KOSHERD_WAIT = 60
# GDM's greeter is up when its session is on seat0 and its shell runs.
GREETER_UP = ("loginctl list-sessions --no-legend | grep -w greeter | grep -qw seat0 && "
              "ps -eo user:32=,comm= | awk -v u=" + USER + " '$2==\"gnome-shell\" && $1!=u' "
              "| grep -q .")


def no_session_reason(greeter: bool, journal: str, ci: bool) -> str:
    """What to say when the account never got a graphical session."""
    broken = not greeter or "Session never registered" in journal
    if broken and ci:
        return ("no graphical session: this runner has no working graphical session "
                "for the VM (GDM's login screen did not stay up), so the desktop "
                "checks cannot run here")
    if broken:
        return ("no graphical session: GDM's login screen did not stay up "
                "(see the gdm journal below and the screenshots)")
    return ("no graphical session: the login screen was up, but signing in with the "
            "test account's name and password did not start a session")


def stage_desktop(st: Stage, machine: Path, outdir: Path, key: Path, gl: bool,
                  tree: Path = REPO, parts: set = frozenset({"session", "filter"})) -> None:
    vm = Machine(machine, outdir, key, gl)
    log = st.log.open("a")

    def sh(script: str, timeout: int = 120, input: str | None = None) -> tuple[int, str]:
        rc, out = vm.sh(script, timeout, input)
        log.write(f"# {script}\n{out}\n[exit {rc}]\n")
        log.flush()
        return rc, out

    try:
        if not st.check("the machine boots with a screen and answers ssh", vm.wait_ssh(),
                        "see desktop-console.log"):
            raise StageError("no ssh")
        vm.shoot("01-boot")
        if not st.check("the machine is set up", sh("test -e /var/lib/kosher/setup-complete")[0] == 0):
            raise StageError("setup was never finished on this machine")

        # kosherd may still be starting when ssh answers, and the first
        # create-user would then time out on D-Bus.
        up = wait_for(lambda: sh(KOSHERD_PING, timeout=20)[0] == 0, KOSHERD_WAIT, step=3)
        if not st.check("kosherd answers on D-Bus", up,
                        sh("systemctl status kosherd --no-pager | tail -20")[1]):
            raise StageError(f"kosherd did not answer on D-Bus within {KOSHERD_WAIT}s")

        # The fixture: a filtered account with a password, and a login
        # screen that asks for a name rather than showing a list, so the
        # keys typed below land in a known field.
        rc, out = sh(f"""set -e
            id {USER} >/dev/null 2>&1 || kosherctl create-user {USER} filtered --full-name 'PR Kid'
            echo '{USER}:{USER_PASSWORD}' | chpasswd
            mkdir -p /etc/dconf/db/gdm.d
            printf '[org/gnome/login-screen]\\ndisable-user-list=true\\n' > /etc/dconf/db/gdm.d/99-pr-test
            dconf update
            systemctl restart gdm""", timeout=180)
        st.check(f"a filtered test account ({USER}) is made", rc == 0, out)

        props: dict = {}
        greeter = False
        for attempt in (1, 2):
            if attempt == 2:
                # Once more from a fresh login screen: on a slow machine
                # the greeter can fail to start the first time, and the
                # keys typed into it are lost.
                log.write("# no session after the first try; GDM is restarted\n")
                sh("systemctl restart gdm")
            up = wait_for(lambda: sh(GREETER_UP)[0] == 0, 180)
            greeter = greeter or up
            # The greeter's session exists before its window does.
            time.sleep(8 if attempt == 1 else 20)
            vm.shoot(f"02-login-screen-{attempt}")
            vm.qmp.type(USER + "\n")
            time.sleep(3 if attempt == 1 else 6)
            vm.shoot(f"03-password-{attempt}")
            vm.qmp.type(USER_PASSWORD + "\n")
            props = wait_for(lambda: session_of(vm, USER), 240)
            if props:
                break
        st.check("the login screen is up on seat0", greeter)
        vm.shoot("04-signed-in")
        log.write(f"# session: {props}\n")
        if not props:
            _, journal = sh("loginctl list-sessions; journalctl -b -u gdm --no-pager | tail -30")
            reason = no_session_reason(greeter, journal, bool(os.environ.get("CI")))
            st.check("the account signs in through GDM", False, f"{reason}\n{journal}")
            raise StageError(reason)
        st.check("the account signs in through GDM", True)
        if props.get("Active") != "yes":
            # The session is listed while still "opening"; one train run
            # read it a moment before it was active and failed on that.
            def active() -> dict:
                p = session_of(vm, USER)
                return p if p.get("Active") == "yes" else {}
            props = wait_for(active, 90) or props
        st.check("the session has a seat (seat0)", props.get("Seat") == "seat0", str(props))
        st.check("the session is active and local",
                 props.get("Active") == "yes" and props.get("Remote") == "no", str(props))
        st.check("the session is Wayland", props.get("Type") == "wayland", str(props))
        sid = props.get("Id", "")

        if "session" in parts:
            # The desktop settles: the shell, then the user's services.
            time.sleep(20)
            vm.shoot("05-desktop")
            state = wait_for(lambda: (lambda r: r[1].strip() if r[1].strip() in ("running", "degraded") else "")(
                vm.user_unit(USER, "is-system-running", "")), 60)
            _, failed = vm.user_unit(USER, "list-units --failed --no-legend --plain", "")
            st.check("the account's user services all started",
                     state == "running" and not failed.strip(),
                     f"state={state or 'starting'}\n{failed}")

            # polkit, asked about the shell's own process, as polkit asks
            # about anything the person starts from the desktop.
            rc, shell_pid = sh(f"pgrep -u {USER} -x gnome-shell | head -1")
            shell_pid = shell_pid.strip()
            rc, out = sh(f"pkcheck --action-id org.kosherlinux.use-store --process {shell_pid}")
            st.check("polkit lets the signed-in account use the Store", rc == 0, out)
            rc, out = sh(f"pkcheck --action-id org.kosherlinux.manage-filter --process {shell_pid}")
            st.check("polkit asks for an administrator before a filter change", rc == 2, f"exit {rc}: {out}")
            # A real password prompt. A filter change from the account's own
            # session (what the admin app does when opened on a child's
            # account) waits on polkit, which puts the administrator's
            # password prompt on the screen. Each prompt that appears is
            # answered there, and counted.
            vm.start_as_user(USER, "pr-test-prompt",
                             f"kosherctl set-mode $(id -u {USER}) filtered --guardian-password '' > /tmp/pr-test-prompt.out 2>&1; echo $? >> /tmp/pr-test-prompt.out")
            prompts, done = 0, ""
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                done = sh("test -s /tmp/pr-test-prompt.out && tail -1 /tmp/pr-test-prompt.out")[1].strip()
                if done:
                    break
                shot = vm.shoot(f"06-password-prompt-{prompts + 1}")
                if prompt_on_screen(shot):
                    prompts += 1
                    vm.qmp.type(ADMIN_PASSWORD + "\n")
                    time.sleep(4)
                else:
                    time.sleep(2)
            vm.shoot("07-after-prompt")
            _, auth = sh("journalctl -b -u polkit --no-pager | grep -i 'authenticate' | tail -5")
            st.check("the administrator's password prompt appears on the screen", prompts > 0,
                     "no prompt was seen in the screenshots")
            st.check("the password typed into it is accepted and the change goes through",
                     done == "0", f"kosherctl exit: {done or 'still waiting'}\n{auth}\n"
                     + sh("cat /tmp/pr-test-prompt.out")[1])
            st.check("one change asks for the password once", prompts <= 1,
                     f"{prompts} prompts for one `kosherctl set-mode`\n{auth}")

            # The apps open in the session and stay open. The account's own
            # apps open without a password; the admin app on a child's account
            # asks for an administrator's, which is what the rule is for.
            for app, cmd, wants_prompt in [("Look", "kosher-look", False),
                                           ("admin app", "kosher-admin", True),
                                           ("My Filter", "kosher-my-filter", False),
                                           ("Store", "kosher-store", False)]:
                unit = f"pr-test-{cmd}"
                vm.start_as_user(USER, unit, cmd)
                time.sleep(12)
                shot = vm.shoot(f"08-{cmd}")
                prompted = prompt_on_screen(shot)
                rc, _ = vm.user_unit(USER, "is-active --quiet", unit)
                _, journal = sh(f"journalctl -b --no-pager _UID=$(id -u {USER}) | grep -A8 'Traceback' | tail -20")
                st.check(f"the {app} opens and stays open", rc == 0 and "Traceback" not in journal,
                         journal or "the window closed by itself")
                if wants_prompt:
                    st.check(f"the {app} asks for an administrator's password", prompted,
                             f"no prompt in {shot.name}")
                else:
                    st.check(f"the {app} opens without asking for a password", not prompted,
                             f"an administrator's password prompt is on the screen: {shot.name}")
                if prompted:
                    vm.qmp.key("esc")
                    time.sleep(2)
                vm.user_unit(USER, "stop", unit)
                time.sleep(2)

        if "filter" in parts:
            # The filter as a browser meets it: many connections at once, the
            # literal block page, then Firefox itself.
            script = f"""
                loaded=0; pages=0; codes=""
                for i in $(seq 16); do
                  ( curl -s -m 30 {BLOCKED_URL} -o /tmp/pr-block-$i.html -w '%{{http_code}}' > /tmp/pr-block-$i.code ) &
                done; wait
                for i in $(seq 16); do
                  c=$(cat /tmp/pr-block-$i.code); codes="$codes $c"
                  case "$c" in 2*|3*) loaded=$((loaded+1));; esac
                  grep -qs 'This page is blocked' /tmp/pr-block-$i.html && pages=$((pages+1))
                done
                echo "loaded=$loaded pages=$pages codes:$codes"
                getent ahostsv4 www.unibet.com | head -1
                curl -s -m 30 -o /dev/null -w 'allowed=%{{http_code}}\\n' {ALLOWED_URL}
                curl -sv -m 30 -o /dev/null {ALLOWED_URL} 2>&1 | grep -i issuer
            """
            rc, reach = sh("curl -s -m 20 -o /dev/null -w '%{http_code}' https://example.com/")
            if reach.strip() != "200":
                st.checks.append({"name": "the filter as a browser meets it", "ok": True,
                                  "detail": "skipped: no internet from the VM"})
                print("  SKIP  the filter checks: no internet from the VM")
            else:
                rc, out = vm.as_user(USER, script, timeout=180)
                log.write(out)
                st.check("16 requests at once to a gambling site: none of them loads it",
                         "loaded=0 " in out, out)
                st.check("and every one of them gets the block page", "pages=16 " in out, out)
                st.check("an ordinary site loads, through the proxy",
                         "allowed=200" in out and "mitmproxy" in out.lower(), out)
                vm.start_as_user(USER, "pr-test-firefox", f"firefox --new-window {BLOCKED_URL}")
                time.sleep(25)
                vm.shoot("09-firefox-blocked")
                rc, activity = sh(f"kosherctl activity --user {USER}")
                st.check("Firefox's traffic goes through the filter",
                         "firefox" in activity.lower() or "mozilla" in activity.lower(), activity[-1200:])
                st.check("the filter records Firefox's visit to the gambling site",
                         "unibet" in activity.lower(), activity[-1200:])
                vm.user_unit(USER, "stop", "pr-test-firefox")

        # Last, since one of them may sign the account out.
        desktop_scripts(st, vm, tree, gl, sh)
    finally:
        log.close()
        vm.stop()


def desktop_scripts(st: Stage, vm: Machine, tree: Path, gl: bool, sh) -> None:
    """The target's scripts/vm-tests/desktop/*.sh, on the desktop machine
    with the test account signed in at the screen, for what only a real
    session can show: whose files are in force, what a sign-in starts.
    Each runs as root with PR_USER (the account at the screen), ADMIN_USER
    and PR_GL (1 when the machine has a GPU), sources ../lib.sh, and
    prints PASS, FAIL and SKIP lines as the suites do."""
    scripts = sorted((tree / "scripts/vm-tests/desktop").glob("*.sh"))
    if not scripts:
        return
    tar = subprocess.run(["tar", "-C", str(tree / "scripts"), "-czf", "-", "vm-tests"],
                         capture_output=True, check=True).stdout
    subprocess.run(vm.ssh_cmd() + ["rm -rf /tmp/kosher-vm-tests && mkdir -p /tmp/kosher-vm-tests"
                                   " && tar -xzf - -C /tmp/kosher-vm-tests --strip-components=1"],
                   input=tar, check=True)
    env = f"PR_USER={USER} ADMIN_USER={ADMIN_USER} PR_GL={1 if gl else 0}"
    for script in scripts:
        rc, out = sh(f"{env} bash /tmp/kosher-vm-tests/desktop/{script.name}", timeout=1500)
        text = re.sub(r"\x1b\[[0-9;]*m", "", out)
        before = len(st.checks)
        for m in re.finditer(r"^\s*(PASS|FAIL|SKIP):?\s+(.*)$", text, re.M):
            st.checks.append({"name": f"{script.stem}: {m.group(2)}", "ok": m.group(1) != "FAIL",
                              "detail": "skipped" if m.group(1) == "SKIP" else ""})
        bad = sum(1 for c in st.checks[before:] if not c["ok"])
        if rc != 0 and not bad:
            st.check(f"{script.stem}: finished", False, f"exit {rc}\n{text[-1500:]}")
            bad = 1
        print(f"  [{st.name}] {script.stem}: {len(st.checks) - before - bad} passed, "
              f"{bad} failed", flush=True)




# --- the suites, several machines at once ----------------------------------------

# How long each suite took on one machine (seconds, measured), so the groups
# come out even. A suite not listed counts as a minute.
SUITE_SECONDS = {"10-core": 12, "20-enforcement": 32, "30-apps": 61, "40-guest": 9,
                 "50-inspect": 16, "60-persistence": 77, "70-categories": 22,
                 "80-hardening": 51, "90-developer": 93}

# Waits that only make a failing check slower, cut short in the fast run.
# --full leaves them as they are. Each one is listed in the report.
FAST_CAPS = {
    "NIX_CONFIG": "connect-timeout = 5\ndownload-attempts = 1",
}


def groups_for(suites: list[str], n: int) -> list[list[str]]:
    """Split the suites into n groups of about equal time, each group in the
    suites' own order."""
    bins: list[list[str]] = [[] for _ in range(max(1, min(n, len(suites))))]
    load = [0] * len(bins)
    for suite in sorted(suites, key=lambda x: -SUITE_SECONDS.get(x, 60)):
        i = load.index(min(load))
        bins[i].append(suite)
        load[i] += SUITE_SECONDS.get(suite, 60)
    return [sorted(b) for b in bins if b]


def parse_registry_image(ref: str) -> tuple[str, str]:
    """NAME[:TAG]@sha256:DIGEST, as a registry reports a pushed image: the
    name the machine switches to and the digest it must then be running."""
    m = re.fullmatch(r"([^@\s]+?)(?::[A-Za-z0-9_][A-Za-z0-9_.-]*)?@(sha256:[0-9a-f]{64})", ref)
    if not m:
        raise SystemExit(f"--registry-image {ref!r}: NAME[:TAG]@sha256:DIGEST")
    return m.group(1), m.group(2)


def parse_shard(shard: str) -> tuple[bool, tuple[int, int] | None]:
    """What one machine of a split run does (CI runs these side by side):
    "all" (the desktop and every suite group, here), "desktop", or
    "suites:K/N" (the K-th of N suite groups). Returns (desktop, (k, n))."""
    if shard in ("", "all"):
        return True, None
    if shard == "desktop":
        return True, (0, 0)
    m = re.fullmatch(r"suites:(\d+)/(\d+)", shard)
    if not m or not 1 <= int(m.group(1)) <= int(m.group(2)):
        raise SystemExit(f"--shard {shard!r}: all, desktop or suites:K/N")
    return False, (int(m.group(1)), int(m.group(2)))


def shard_groups(suites: list[str], part: tuple[int, int]) -> list[list[str]]:
    """The suite groups a shard runs: none for the desktop's, the K-th of N
    for a suites shard. Every runner works the groups out from the same
    selection, so the K-th is the same on all of them."""
    k, n = part
    if n == 0:
        return []
    groups = groups_for(suites, n)
    groups.sort(key=lambda g: (-sum(SUITE_SECONDS.get(x, 60) for x in g), g))
    return [groups[k - 1]] if k <= len(groups) else []


def child_overlay(base: Path, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.unlink(missing_ok=True)
    subprocess.run(["qemu-img", "create", "-q", "-f", "qcow2", "-b", str(base.resolve()),
                    "-F", "qcow2", str(path)], check=True)
    return path


def stage_suites(st: Stage, machine: Path, workdir: Path, key: Path, tree: Path,
                 suites: list[str], caps: dict) -> None:
    """Some of the target's scripts/vm-tests, one after another, on a
    machine of their own (an overlay of the set-up machine)."""
    overlay = child_overlay(machine, workdir / "machine.qcow2")
    vm = Machine(overlay, workdir, key, gl=False, console="console.log", mem=3072)
    st.timings = {}
    try:
        if not st.check("the machine boots and answers ssh", vm.wait_ssh()):
            raise StageError("no ssh")
        tar = subprocess.run(["tar", "-C", str(tree / "scripts"), "-czf", "-", "vm-tests"],
                             capture_output=True, check=True).stdout
        subprocess.run(vm.ssh_cmd() + ["rm -rf /tmp/kosher-vm-tests && mkdir -p /tmp/kosher-vm-tests && tar -xzf - -C /tmp/kosher-vm-tests --strip-components=1"],
                       input=tar, check=True)
        exports = " ".join(f"{k}={shlex.quote(v)}" for k, v in caps.items())
        for suite in suites:
            t0 = time.monotonic()
            with st.log.open("a") as fh:
                p = subprocess.Popen(vm.ssh_cmd() + [f"{exports} bash /tmp/kosher-vm-tests/run.sh {suite}"],
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                     errors="replace")
                assert p.stdout is not None
                out = []
                for line in p.stdout:
                    fh.write(line)
                    out.append(line)
                rc = p.wait()
            st.timings[suite] = round(time.monotonic() - t0)
            text = re.sub(r"\x1b\[[0-9;]*m", "", "".join(out))
            before = len(st.checks)
            for m in re.finditer(r"^\s*(PASS|FAIL|SKIP):?\s+(.*)$", text, re.M):
                st.checks.append({"name": f"{suite}: {m.group(2)}", "ok": m.group(1) != "FAIL",
                                  "detail": "skipped" if m.group(1) == "SKIP" else ""})
            bad = sum(1 for c in st.checks[before:] if not c["ok"])
            if rc != 0 and not bad:
                st.check(f"{suite}: run.sh finished", False, f"exit {rc}")
                bad = 1
            print(f"  [{st.name}] {suite}: {len(st.checks) - before - bad} passed, {bad} failed "
                  f"in {st.timings[suite]}s", flush=True)
    finally:
        vm.stop()
        overlay.unlink(missing_ok=True)


# --- the set-up machine: a cached base, switched to the change's image ------------

# One cache for every checkout and worktree on the host, not one per tree:
# a base is about 11 GB, and five worktrees each making their own for the
# same master filled the disk. A run holds CACHE_LOCK for as long as it
# lives, so runs that share the cache, the registry port and the host's
# memory take turns instead of colliding.
CACHE = Path(os.environ.get("KOSHER_PR_TEST_CACHE")
             or Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "kosher-pr-test")
CACHE_LOCK = CACHE / "lock"
REGISTRY = "kosher-pr-test-registry"
REGISTRY_PORT = 5078
# The family's own repository, signed with the KosherOS key (ci.yml).
RELEASE_IMAGE = "ghcr.io/aareman/kosher-linux"
BASE_MAX_AGE_DAYS = 7


def git(*args: str) -> str:
    return subprocess.run(["git", "-C", str(REPO), *args], capture_output=True, text=True,
                          check=True).stdout.strip()


def changes_against_master(sha: str, env: dict, log: Path) -> tuple[list[str], str, str]:
    """Paths the target changes since it left master, the Containerfile's
    diff, and master's commit."""
    run_logged(["git", "-C", str(REPO), "fetch", "--quiet", "origin", "master"], log, env)
    master = git("rev-parse", "FETCH_HEAD")
    base = git("merge-base", sha, master)
    if base == sha:
        return [], "", master
    changed = git("diff", "--name-only", base, sha).splitlines()
    cf = git("diff", "-U0", base, sha, "--", "os-image/Containerfile")
    return changed, cf, master


def registry_up(env: dict, log: Path) -> None:
    """The harness's own registry, so a machine can pull the change's image
    (10.0.2.2 is the host from inside QEMU). Its blobs persist between runs,
    so only the layers a change made are pushed again."""
    if subprocess.run(["podman", "container", "exists", REGISTRY]).returncode != 0:
        run_logged(["podman", "create", "--name", REGISTRY, "-p", f"127.0.0.1:{REGISTRY_PORT}:5000",
                    "-v", f"{REGISTRY}:/var/lib/registry", "docker.io/library/registry:2"], log, env)
    run_logged(["podman", "start", REGISTRY], log, env)
    for _ in range(40):
        try:
            with socket.create_connection(("127.0.0.1", REGISTRY_PORT), timeout=1):
                return
        except OSError:
            time.sleep(0.5)
    raise StageError("the harness registry did not start")


def push(image: str, tag: str, env: dict, log: Path) -> str:
    ref = f"127.0.0.1:{REGISTRY_PORT}/kosher-linux:{tag}"
    digestfile = log.parent / f"{tag}.digest"
    if run_logged(["podman", "push", "--tls-verify=false", "--digestfile", str(digestfile),
                   image, ref], log, env, timeout=1800) != 0:
        raise StageError(f"could not push {image}")
    return digestfile.read_text().strip()


SWITCH_SETUP = f"""set -e
mkdir -p /etc/containers/registries.conf.d
printf '[[registry]]\\nlocation = "10.0.2.2:{REGISTRY_PORT}"\\ninsecure = true\\n' \\
    > /etc/containers/registries.conf.d/50-pr-test.conf
python3 -c 'import json; p = "/etc/containers/policy.json"; d = json.load(open(p)); d.setdefault("transports", {{}}).setdefault("docker", {{}})["10.0.2.2:{REGISTRY_PORT}"] = [{{"type": "insecureAcceptAnything"}}]; json.dump(d, open(p, "w"), indent=4)'
"""


CLEAN_SWITCH = f"""set -e
rm -f /etc/containers/registries.conf.d/50-pr-test.conf
python3 -c 'import json; p = "/etc/containers/policy.json"; d = json.load(open(p)); d.get("transports", {{}}).get("docker", {{}}).pop("10.0.2.2:{REGISTRY_PORT}", None); json.dump(d, open(p, "w"), indent=4)'
"""


def local_ref(tag: str) -> str:
    """The harness's registry as the machine sees it (10.0.2.2 is the host)."""
    return f"10.0.2.2:{REGISTRY_PORT}/kosher-linux:{tag}"


def switch_machine(st: Stage, machine: Path, workdir: Path, key: Path, ref: str,
                   digest: str, *, local: bool = True) -> None:
    """Boot the machine, `bootc switch` it to the pushed image, reboot into
    it and check it is running that image: the update path a family's
    machine takes, from the set-up state the cached base holds.

    REF is in the harness's own registry (local_ref), which the machine is
    told to trust for the switch and to forget afterwards, or the image's
    name in a public one (--registry-image), pinned to DIGEST, which the
    machine's own policy already accepts."""
    vm = Machine(machine, workdir, key, gl=False, console="switch-console.log")
    target = ref if local else f"{ref}@{digest}"
    # A switch to the release repository is held to the signature policy,
    # as kosherd's own updates are (updates.py): the machine then runs a
    # verified deployment, which is what 80-hardening [F10] looks for. The
    # harness's own images (the local registry, kosher-linux-ci) are
    # unsigned, and the policy takes them as it takes any other registry.
    enforce = ["--enforce-container-sigpolicy"] if ref == RELEASE_IMAGE else []
    try:
        if not st.check("the set-up machine boots and answers ssh", vm.wait_ssh()):
            raise StageError("no ssh")
        if not local:
            # The machine's own resolver (kosher-dns) is up a few seconds
            # after sshd, and a switch by name before it answers fails
            # with "Temporary failure in name resolution". The harness's
            # registry is an address, so it never waited.
            host = ref.split("/", 1)[0]
            rc, out = vm.sh(f"for i in $(seq 45); do getent hosts {host} >/dev/null && exit 0; "
                            "sleep 2; done; exit 1", timeout=120)
            if not st.check(f"the machine resolves {host}", rc == 0, out):
                raise StageError("no name resolution")
        rc, out = vm.sh((SWITCH_SETUP if local else "set -e\n")
                        + f"bootc switch {' '.join(enforce + [target])}", timeout=1800)
        with st.log.open("a") as fh:
            fh.write(out)
        if not st.check("bootc switch stages the image", rc == 0, out[-1500:]):
            raise StageError("switch failed")
        vm.sh("systemctl reboot", timeout=20)
        time.sleep(10)
        if not st.check("it boots into it", vm.wait_ssh()):
            raise StageError("no ssh after the switch")
        _, status = vm.sh("bootc status --json")
        try:
            booted = json.loads(status)["status"]["booted"]["image"]["imageDigest"]
        except (ValueError, KeyError, TypeError):
            booted = ""
        st.check("the machine runs the change's image", booted == digest,
                 f"booted {booted or '?'}, pushed {digest}")
        # Take the harness's registry back out: a suite checks that no
        # development registry is trusted (N6), and the machine must look
        # like one that was updated, not one with a test hook left in it.
        if local:
            rc, out = vm.sh(CLEAN_SWITCH)
            st.check("the harness registry is removed from the machine's trust again",
                     rc == 0, out)
    finally:
        vm.stop()


def key_fingerprint(pub: str) -> str:
    """An ssh public key's SHA256 fingerprint, as ssh-keygen -l prints it."""
    blob = base64.b64decode(pub.split()[1])
    return "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")


def base_key_problem(meta: dict, fingerprint: str) -> str:
    """Why a base cannot be signed in to with the cache's key, or "".

    The image writes root's authorized_keys from the boot's credential
    only when the file is missing, so a set-up base keeps the key it was
    first booted with. A base made with another key would only time out
    on ssh."""
    had = meta.get("ssh_key")
    if had == fingerprint:
        return ""
    if not had:
        return "its ssh key was not recorded when it was made, so it may not let this run in"
    return f"it was set up with another ssh key ({had}; the cache's key is {fingerprint})"


def find_base(master: str, fingerprint: str) -> tuple[Path | None, str]:
    """A cached set-up base for master's commit, or a recent one, that
    takes the cache's ssh key."""
    bases = []
    for meta in CACHE.glob("base-*/meta.json"):
        try:
            m = json.loads(meta.read_text())
        except ValueError:
            continue
        if not (meta.parent / "setup.qcow2").exists():
            continue
        problem = base_key_problem(m, fingerprint)
        if problem:
            print(f"  not using the cached base {meta.parent.name}: {problem}; "
                  "a fresh base is made instead", flush=True)
            continue
        bases.append((m.get("created", 0), m, meta.parent))
    bases.sort(key=lambda b: b[0], reverse=True)
    for _created, m, d in bases:
        if m.get("master") == master:
            return d, f"the cached base for master {master[:9]}, switched to this change's image"
    if bases and time.time() - bases[0][0] < BASE_MAX_AGE_DAYS * 86400:
        m, d = bases[0][1], bases[0][2]
        return d, (f"the cached base for master {m['master'][:9]} "
                   f"({(time.time() - m['created']) / 3600:.0f} h old), switched to this change's "
                   f"image; a base is remade after {BASE_MAX_AGE_DAYS} days or with --fresh-base")
    return None, ""


def prune_bases(keep: Path) -> None:
    """Keep the base in use and the newest other one; each is about 10 GB."""
    others = sorted((d for d in CACHE.glob("base-*") if d != keep),
                    key=lambda d: d.stat().st_mtime, reverse=True)
    for d in others[1:]:
        shutil.rmtree(d, ignore_errors=True)


def make_base(st: Stage, master: str, sha: str, image: str, key: Path, env: dict,
              registry: tuple[str, str] | None = None) -> Path:
    """Master's image, installed and set up by the wizard, then switched to
    itself through the registry, so later switches pull only the layers a
    change makes. Once per master commit (or week).

    With REGISTRY (CI, where master's image is already public), the switch
    is to that image there, and the base's meta records its digest."""
    base_image = image if master == sha else "localhost/kosher-linux:pr-test-base"
    if registry and base_image == image:
        stage_pull(st, registry, image, env)
    fingerprint = key_fingerprint(key.with_suffix(".pub").read_text())
    if base_image != image:
        LEFTOVERS.image(base_image)
        btree = CACHE / "base-src"
        subprocess.run(["git", "-C", str(REPO), "worktree", "remove", "--force", str(btree)],
                       capture_output=True)
        run_logged(["git", "-C", str(REPO), "worktree", "add", "--detach", str(btree), master],
                   st.log, env)
        try:
            stage_build(st, btree, base_image, env)
        finally:
            subprocess.run(["git", "-C", str(REPO), "worktree", "remove", "--force", str(btree)],
                           capture_output=True)
    image_id = subprocess.run(["podman", "image", "inspect", base_image, "--format", "{{.Id}}"],
                              capture_output=True, text=True).stdout.strip()
    # Named for master's revision: a rebuild of the same revision can get a
    # new image id, and a disk made from either is the same machine.
    base = CACHE / f"base-{master[:12]}"
    base.mkdir(parents=True, exist_ok=True)
    setup = base / "setup.qcow2"
    # A base an interrupted run left half made carries on from where it got.
    stamp = base / ".image-id"
    if not ((base / "disk.qcow2").exists() and stamp.exists()):
        for f in base.iterdir():
            shutil.rmtree(f) if f.is_dir() else f.unlink()
        stage_disk(st, base_image, base / "disk.qcow2", env)
    else:
        st.check("a bootable disk is made without root (kept from an earlier run)", True)
    # The disk holds no key, but a set-up machine keeps the first key it
    # was booted with: one made with another key is set up again.
    key_file = base / "ssh-key"
    if (setup.exists() and (not key_file.exists()
                            or key_file.read_text().strip() != fingerprint)):
        for f in ("setup.qcow2", "boot.log", "boot-console.log", "meta.json"):
            (base / f).unlink(missing_ok=True)
    key_file.write_text(fingerprint + "\n")
    boot_log = base / "boot.log"
    if not (setup.exists() and boot_log.exists()):
        e = dict(env, KOSHER_IMAGE=base_image, PYTHONUNBUFFERED="1")
        run_logged([sys.executable, str(REPO / "scripts/boot-test.py"), str(base / "disk.qcow2"),
                    "--log", str(base / "boot-console.log"), "--keep-overlay", str(setup)],
                   boot_log, e, timeout=1800)
    if not re.search(r"PASS\s+setup completes", boot_log.read_text()):
        child_overlay(base / "disk.qcow2", setup)
        vm = Machine(setup, base, key, gl=False, console="setup-console.log")
        try:
            vm.wait_ssh()
            rc, out = vm.sh(f"kosherctl setup --username {ADMIN_USER} --full-name 'Boot Test' "
                            "--password-stdin", input=ADMIN_PASSWORD + "\n", timeout=180)
        finally:
            vm.stop()
        st.check("the base is set up (over ssh: the wizard did not finish)", rc == 0, out)
    else:
        st.check("the base is set up by the wizard", True)
    meta = {"master": master, "image_id": stamp.read_text().strip(), "created": time.time(),
            "ssh_key": fingerprint}
    if registry and base_image == image:
        switch_machine(st, setup, base, key, registry[0], registry[1], local=False)
        meta["digest"] = registry[1]
    else:
        registry_up(env, st.log)
        digest = push(base_image, "base", env, st.log)
        switch_machine(st, setup, base, key, local_ref("base"), digest)
    (base / "meta.json").write_text(json.dumps(meta))
    if base_image != image:
        keep_build_cache(base_image)
    return base



# --- known failures ---------------------------------------------------------------

def family(stage_name: str) -> str:
    """Where a stage's checks are listed in the baseline."""
    if stage_name.startswith("suites"):
        return "suites"
    return stage_name


def load_baseline(path: Path) -> list[dict]:
    entries = []
    if not path.exists():
        return entries
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r"^(\w[\w-]*):\s+(.*?)\s+#(\d+)\s*$", line)
        if m:
            entries.append({"family": m.group(1), "check": m.group(2), "issue": int(m.group(3))})
    return entries


def classify(stages: list[dict], baseline: list[dict]) -> dict:
    """Every failure is new or known; a known one that passed is fixed.

    A stage that ended on an error, with no failed check to show for it,
    counts as a new failure: the run did not get to say."""
    new, known, fixed = [], [], []
    seen_pass: dict[str, list[str]] = {}
    for s in stages:
        fam = family(s["name"])
        for c in s["checks"]:
            if c["ok"] and c.get("detail") != "skipped":
                seen_pass.setdefault(fam, []).append(c["name"])
        failed = [c for c in s["checks"] if not c["ok"]]
        for c in failed:
            match = next((b for b in baseline if b["family"] == fam
                          and c["name"].startswith(b["check"])), None)
            item = {"where": fam, "check": c["name"], "stage": s["name"]}
            if match:
                known.append(dict(item, issue=match["issue"]))
            else:
                new.append(item)
        if s["status"] == "failed" and s.get("note") and not failed:
            new.append({"where": fam, "check": s["note"], "stage": s["name"]})
    for b in baseline:
        if any(n.startswith(b["check"]) for n in seen_pass.get(b["family"], [])) and \
                not any(k["where"] == b["family"] and k["check"].startswith(b["check"]) for k in known):
            fixed.append(b)
    return {"new": new, "known": known, "fixed": fixed}


# --- the report -----------------------------------------------------------------

def write_report(outdir: Path, summary: dict) -> None:
    (outdir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    sel = summary.get("selection") or {}
    lines = [f"# VM test of {summary['target']} ({summary['commit'][:9]})", "",
             f"Result: **{summary['result']}**, in {summary['seconds'] / 60:.1f} minutes "
             f"({summary.get('mode', '')} run).", ""]
    if sel:
        lines += ["What runs, and why:", ""]
        lines += [f"- {r}" for r in sel.get("reasons", [])] or ["- (nothing)"]
        lines += ["", f"Selected: fresh install {'yes' if sel.get('boot') else 'no'}; "
                  f"desktop {', '.join(sel.get('desktop', [])) or 'no'}; "
                  f"suites {', '.join(sel.get('suites', [])) or 'none'}.", ""]
    if summary.get("base"):
        lines += [f"Machine: {summary['base']}.", ""]
    if summary.get("caps"):
        lines += ["Waits cut short in this run: " + "; ".join(summary["caps"]) + ".", ""]
    verdict = summary.get("failures") or {}
    if verdict:
        lines += [f"New failures: {len(verdict['new'])}. Known failures: {len(verdict['known'])}. "
                  f"Known failures that now pass: {len(verdict['fixed'])}.", ""]
        if verdict["new"]:
            lines += ["## New failures (these fail the run)", ""]
            lines += [f"- {f['where']}: {f['check']}" for f in verdict["new"]] + [""]
        if verdict["fixed"]:
            lines += ["## Known failures that now pass (remove them from "
                      "scripts/vm-tests/known-failures.txt)", ""]
            lines += [f"- {f['family']}: {f['check']} (#{f['issue']})" for f in verdict["fixed"]] + [""]
        if verdict["known"]:
            lines += ["## Known failures (master has them too; each has an issue)", ""]
            lines += [f"- {f['where']}: {f['check']} (#{f['issue']})" for f in verdict["known"]] + [""]
    lines += ["| Stage | Result | Time | Checks |", "|---|---|---|---|"]
    for s in summary["stages"]:
        n = len(s["checks"])
        bad = sum(1 for c in s["checks"] if not c["ok"])
        lines.append(f"| {s['name']} | {s['status']} | {s['seconds'] / 60:.1f} min | "
                     f"{n - bad} of {n} passed |")
    timings = {k: v for s in summary["stages"] for k, v in (s.get("timings") or {}).items()}
    if timings:
        lines += ["", "Suites: " + ", ".join(f"{k} {v}s" for k, v in timings.items())]
    lines += ["", f"Logs, consoles and screenshots: `{outdir}`", ""]
    (outdir / "report.md").write_text("\n".join(lines))


def machines_to_use(requested: int) -> int:
    """Machines for the suites that fit beside everything else on the host.

    A suite machine is given 3 GB and touches about 2.5 of it; the desktop's
    about 3.5. 3 GB is left spare. Other VMs on the host count against what
    is available, so a busy host gets fewer machines rather than swapping.
    """
    try:
        avail = next(int(line.split()[1]) for line in open("/proc/meminfo")
                     if line.startswith("MemAvailable")) / 1024 / 1024
    except (OSError, StopIteration):
        return requested
    return max(1, min(requested, int((avail - 3 - 3.5) // 2.5)))


CACHE_TAG = "localhost/kosher-linux:pr-test-cache"


def image_id(ref: str) -> str:
    return subprocess.run(["podman", "image", "inspect", ref, "--format", "{{.Id}}"],
                          capture_output=True, text=True).stdout.strip()


def keep_build_cache(image: str) -> None:
    """Drop the run's tag but keep its layers for the next build.

    Removing the last tag on an image removes its build cache with it, and
    the next build then starts from nothing (twenty minutes instead of
    half of one). So the newest build holds one harness tag, and the one
    it replaces is removed: one image's worth of layers, not one per run.
    """
    old = image_id(CACHE_TAG)
    new = image_id(image)
    if new:
        subprocess.run(["podman", "tag", image, CACHE_TAG], capture_output=True)
    subprocess.run(["podman", "rmi", "--ignore", image], capture_output=True)
    if old and old != new:
        subprocess.run(["podman", "rmi", "--ignore", old], capture_output=True)


class Done(Exception):
    """Nothing more to run."""


class Stopped(KeyboardInterrupt):
    """SIGTERM or SIGINT: the run stops, and cleans up after itself."""

    def __init__(self, signum: int):
        super().__init__(signal.Signals(signum).name)
        self.signum = signum


def on_signal(signum, _frame) -> None:
    if LEFTOVERS.done or getattr(on_signal, "seen", None):
        print(f"\npr-test: {signal.Signals(signum).name} again; already stopping", flush=True)
        return
    on_signal.seen = signum
    print(f"\npr-test: {signal.Signals(signum).name}; stopping and cleaning up", flush=True)
    # The machines and builds stop first, so the threads waiting on them
    # end and nothing holds the disks open.
    STOPPING.set()
    LEFTOVERS.stop_all()
    raise Stopped(signum)


def main() -> int:
    from pr_test_select import select

    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                     formatter_class=argparse.RawDescriptionHelpFormatter,
                                     epilog=__doc__.split("\n\n", 1)[1])
    parser.add_argument("target", nargs="?", default="HEAD",
                        help="pull request number, branch, or HEAD (default)")
    parser.add_argument("--id", help="name of the run's directory under build/pr-test")
    parser.add_argument("--full", action="store_true",
                        help="a fresh install of the change's own image and every check")
    parser.add_argument("--all", action="store_true",
                        help="every check whatever the change touches, from the set-up base")
    parser.add_argument("--direct", action=argparse.BooleanOptionalAction,
                        default=bool(os.environ.get("CI")),
                        help="no cached base: a disk from the change's own image, set up over "
                             "ssh unless a fresh install is selected (the default in CI)")
    parser.add_argument("--prefer-base", action="store_true",
                        help="the cached base when there is a usable one, else --direct (a CI "
                             "runner, which may or may not have restored master's base)")
    parser.add_argument("--base-only", action="store_true",
                        help="make the cached base for master, or check it is there, and stop "
                             "(ci.yml, which then saves it for the runners)")
    parser.add_argument("--fresh-base", action="store_true",
                        help="make a new cached base from master even if a recent one exists")
    parser.add_argument("--vms", type=int, default=int(os.environ.get("KOSHER_PR_TEST_VMS", "3")),
                        help="machines for the suites, besides the desktop's (default 3, "
                             "or KOSHER_PR_TEST_VMS)")
    parser.add_argument("--image", help="test this already-built image; skips the build")
    parser.add_argument("--registry-image", metavar="NAME@DIGEST",
                        help="the change's image, already in a registry the machine can reach "
                             "(CI pushes it to ghcr): no build, and the machine switches to it "
                             "there, not through the harness's registry")
    parser.add_argument("--shard", default="all",
                        help="one part of a split run: all, desktop, or suites:K/N (CI)")
    parser.add_argument("--keep", action="store_true",
                        help="keep everything the run made: the machines, the disks, bcvk's "
                             "temp disk, the worktree and the image tag")
    parser.add_argument("--gl", action="store_true", default=bool(os.environ.get("KOSHER_GL")),
                        help="a virgl GPU for the desktop stage (KOSHER_GL=1); needs the host's QEMU")
    args = parser.parse_args()
    LEFTOVERS.keep = args.keep
    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    atexit.register(LEFTOVERS.clean)

    run_id = args.id or (f"pr-{args.target}" if args.target.isdigit()
                         else re.sub(r"[^A-Za-z0-9_.-]", "-", args.target))
    outdir = REPO / "build" / "pr-test" / run_id
    if outdir.exists():
        for child in outdir.iterdir():
            if child.name != "src":
                shutil.rmtree(child) if child.is_dir() else child.unlink()
    outdir.mkdir(parents=True, exist_ok=True)
    CACHE.mkdir(parents=True, exist_ok=True)
    lock_file = CACHE_LOCK.open("a")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print(f"Another pr-test is using {CACHE}; waiting for it to finish.", flush=True)
        fcntl.flock(lock_file, fcntl.LOCK_EX)
    pruned = prune_dangling()
    if pruned:
        print(f"Removed {len(pruned)} dangling images over a day old: {' '.join(pruned)}",
              flush=True)
    env = sudo_trap(outdir)
    registry = parse_registry_image(args.registry_image) if args.registry_image else None
    image = args.image or f"localhost/kosher-linux:pr-test-{run_id.lower()}"
    if not args.image:
        LEFTOVERS.image(image)
    disk = outdir / "disk" / "disk.qcow2"
    machine = outdir / "machine.qcow2"
    for p in (machine, outdir / "desktop.qcow2", disk.parent, outdir / "qmp.sock",
              outdir / "vnc.sock"):
        LEFTOVERS.path(p)
    key = CACHE / "id_ed25519"  # one key for the runs and the cached bases they boot
    if not key.exists():
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "pr-test",
                        "-f", str(key)], check=True)

    want_desktop, part = parse_shard(args.shard)
    started = time.monotonic()
    summary = {"target": args.target, "id": run_id, "commit": "",
               "image": args.registry_image or image,
               "image_id": "", "started": datetime.now(timezone.utc).isoformat(timespec="seconds"),
               "gl": args.gl, "stages": [], "result": "failed", "seconds": 0,
               "mode": "full" if args.full else ("direct" if args.direct else "fast")}
    stages: list[Stage] = []
    lock = threading.Lock()
    tree = REPO
    workers: list[threading.Thread] = []
    stopped = 0

    def stage(name: str) -> Stage:
        s = Stage(name, outdir)
        with lock:
            stages.append(s)
        return s

    def ok(*names: str) -> bool:
        return all(s.status in ("passed", "skipped") for s in stages if s.name in names)

    pre = stage("preflight")
    sel = None
    try:
        with pre:
            if not os.access("/dev/kvm", os.R_OK | os.W_OK):
                pre.check("/dev/kvm is usable", False,
                          "this harness needs KVM; without it a boot takes hours under emulation")
                raise StageError("no KVM")
            pre.check("/dev/kvm is usable", True)
            rc = subprocess.run([sys.executable, str(REPO / "scripts/check-no-sudo.py")],
                                capture_output=True, text=True, cwd=REPO)
            pre.check("no recipe the harness calls needs sudo", rc.returncode == 0,
                      rc.stdout + rc.stderr)
            if rc.returncode != 0:
                raise StageError("a recipe would need sudo")
            tree, sha = resolve_target(args.target, outdir, env, pre.log)
            if tree != REPO:
                LEFTOVERS.worktrees.append(tree)
            summary["commit"] = sha
            pre.check(f"the target is {sha[:9]}", True)
            changed, cf_diff, master = changes_against_master(sha, env, pre.log)
            sel = select(changed, cf_diff, everything=args.all, full=args.full)
            summary["selection"] = sel.as_dict()
            summary["changed"] = changed
            print("  what runs, and why:")
            for r in sel.reasons:
                print(f"    {r}")
        if pre.status != "passed" or sel is None:
            raise Done
        if sel.empty and not args.base_only:
            raise Done
        if part is not None and not (sel.desktop and want_desktop) \
                and not shard_groups(sel.ordered_suites(), part):
            # Before the disk: this runner's part of the split has no checks
            # in what the change selects.
            print(f"\n  nothing for shard {args.shard} in this selection", flush=True)
            raise Done
        caps = {} if args.full else FAST_CAPS
        summary["caps"] = [f"{k}={v!r}" for k, v in caps.items()]

        if not args.image and not registry:
            with stage("build") as st:
                stage_build(st, tree, image, env)
            if not ok("build"):
                raise Done

        fingerprint = key_fingerprint(key.with_suffix(".pub").read_text())
        direct = args.direct
        if args.prefer_base and not args.base_only:
            # A CI runner: master's base from the cache if one was restored
            # (and is recent enough), else the install every run used to do.
            found, _ = find_base(master, fingerprint)
            direct = found is None
            if direct:
                print("  no usable cached base: a fresh disk from the change's image", flush=True)
        if args.base_only:
            direct = False
        if sel.boot or direct:
            # The change's own image, installed: the path every run took before.
            if registry:
                with stage("pull") as st:
                    stage_pull(st, registry, image, env)
                if not ok("pull"):
                    raise Done
            with stage("disk") as st:
                stage_disk(st, image, disk, env)
            if not ok("disk"):
                raise Done
            if sel.boot:
                with stage("boot") as st:
                    stage_boot(st, image, disk, machine, outdir, env)
            if not machine.exists():
                with stage("setup-over-ssh") as st:
                    stage_setup_over_ssh(st, disk, machine, outdir, key)
            summary["base"] = "a fresh install of this change's image"
        else:
            base, why = (None, "") if args.fresh_base else find_base(master, fingerprint)
            if base is None:
                with stage("base") as st:
                    base = make_base(st, master, sha, image, key, env, registry)
                if not ok("base"):
                    raise Done
                why = f"a new cached base for master {master[:9]}, set up by the boot test"
            summary["base"] = why
            print(f"  machine: {why}")
            prune_bases(base)
            if args.base_only:
                print(f"  the base is at {base}; nothing more was asked", flush=True)
                raise Done
            child_overlay(base / "setup.qcow2", machine)
            meta = json.loads((base / "meta.json").read_text())
            if registry:
                # The base already runs this very image (a change to nothing
                # the image is built from tests master's own): no switch.
                current = meta.get("digest") == registry[1]
            else:
                target_id = subprocess.run(["podman", "image", "inspect", image, "--format",
                                            "{{.Id}}"], capture_output=True, text=True).stdout.strip()
                current = target_id == meta.get("image_id")
            if not current:
                with stage("switch") as st:
                    if registry:
                        switch_machine(st, machine, outdir / "switch", key, registry[0],
                                       registry[1], local=False)
                    else:
                        registry_up(env, st.log)
                        tag = f"pr-test-{run_id.lower()}"
                        digest = push(image, tag, env, st.log)
                        switch_machine(st, machine, outdir / "switch", key, local_ref(tag), digest)
                if not ok("switch"):
                    raise Done

        if not machine.exists():
            raise Done

        # From here on, every machine is an overlay of the set-up one, all at once.
        if sel.desktop and want_desktop:
            def desktop():
                with stage("desktop") as st:
                    stage_desktop(st, child_overlay(machine, outdir / "desktop.qcow2"),
                                  outdir, key, args.gl, tree=tree, parts=sel.desktop)
            workers.append(threading.Thread(target=desktop))
        if part is None:
            groups = groups_for(sel.ordered_suites(), machines_to_use(args.vms))
            # The longest group starts first, so the machines finish together.
            groups.sort(key=lambda g: -sum(SUITE_SECONDS.get(x, 60) for x in g))
        else:
            groups = shard_groups(sel.ordered_suites(), part)
        for i, group in enumerate(groups, 1):
            def suites(group=group, i=i):
                with stage(f"suites-{i}") as st:
                    stage_suites(st, machine, outdir / f"suites-{i}", key, tree, group, caps)
            workers.append(threading.Thread(target=suites))
        print(f"\n  {len(workers)} machines at once: desktop "
              f"{'+'.join(sorted(sel.desktop)) or 'no'}; suites {groups}", flush=True)
        for w in workers:
            w.start()
            time.sleep(2)
        for w in workers:
            w.join()
        summary["image_id"] = subprocess.run(
            ["podman", "image", "inspect", image, "--format", "{{.Id}}"],
            capture_output=True, text=True).stdout.strip()
    except Done:
        pass
    except Stopped as e:
        stopped = e.signum
        print(f"\nstopped by {signal.Signals(e.signum).name}")
    except KeyboardInterrupt:
        stopped = signal.SIGINT
        print("\ninterrupted")
    finally:
        # Threads whose machines were stopped end on their own; their
        # files are removed only once they have.
        for w in workers:
            if w.is_alive():
                w.join(timeout=180)
        sudo_log = outdir / "sudo-attempts.log"
        if sudo_log.exists():
            s = Stage("no-sudo", outdir)
            s.check("nothing ran sudo", False, sudo_log.read_text())
            s.status = "failed"
            stages.append(s)
        summary["stages"] = [s.as_dict() for s in stages]
        ran = [s for s in stages if s.status != "skipped"]
        baseline_file = tree / "scripts/vm-tests/known-failures.txt"
        if not baseline_file.exists():
            baseline_file = REPO / "scripts/vm-tests/known-failures.txt"
        verdict = classify(summary["stages"], load_baseline(baseline_file))
        summary["failures"] = verdict
        # Passed means nothing failed that master does not already fail on.
        summary["result"] = "passed" if ran and not verdict["new"] else "failed"
        summary["seconds"] = round(time.monotonic() - started)
        write_report(outdir, summary)
        removed = LEFTOVERS.clean()
        if removed:
            print("\nremoved what the run made: " + ", ".join(
                str(r).replace(str(outdir) + "/", "") for r in removed), flush=True)

    print("\n" + (outdir / "report.md").read_text())
    if stopped:
        return 128 + stopped
    return 0 if summary["result"] == "passed" else 1


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    try:
        sys.exit(main())
    except Stopped as stop:  # before the run had started; atexit cleans up
        sys.exit(128 + stop.signum)
