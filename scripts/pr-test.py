#!/usr/bin/env python3
"""Test a pull request, a branch or this checkout in a real VM, without sudo.

    scripts/pr-test.py [TARGET] [--id ID] [--only STAGES] [--keep] [--gl]

TARGET is a pull request number, a branch on origin, or HEAD (the default:
this checkout as it is, which is what CI hands over). Anything other than
HEAD is checked out into a temporary worktree, so the checkout you are
working in is left alone.

The stages, in order:

  build    `just build` in the target's tree, tagged for this run only
  disk     a qcow2 made from that image without root (scripts/rootless-disk.sh)
  boot     scripts/boot-test.py: power-on, the first-boot wizard over the
           serial line, the administrator's filtering
  desktop  the same machine booted again with a screen: a test account
           signs in through GDM, and its session is checked for a seat, the
           polkit answers, a real password prompt, the apps and the user
           services, and the filter as Firefox meets it
  suites   the target's own scripts/vm-tests, as root over ssh

Everything lands in build/pr-test/<ID>/: summary.json (for machines),
report.md (for people), each stage's log, the serial consoles and the
screenshots. The exit status is non-zero when anything failed.

Nothing here runs sudo. A `sudo` that fails loudly is put first on the PATH
of every command the harness starts, and the just recipes it calls are
checked beforehand (scripts/check-no-sudo.py).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
STAGES = ["build", "disk", "boot", "desktop", "suites"]

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
        proc = subprocess.Popen(cmd, cwd=cwd, env=env, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, errors="replace", bufsize=1)
        deadline = time.monotonic() + timeout if timeout else None
        assert proc.stdout is not None
        for line in proc.stdout:
            fh.write(line)
            sys.stdout.write("    " + line)
            if deadline and time.monotonic() > deadline:
                proc.kill()
                fh.write(f"pr-test: timed out after {timeout}s\n")
                break
        return proc.wait()


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
        self.sockdir = Path(tempfile.mkdtemp(prefix="kpt-"))
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
        self.proc = subprocess.Popen(cmd, stdout=(outdir / "qemu.log").open("w"),
                                     stderr=subprocess.STDOUT)
        self.qmp = QMP(self.qmp_path)

    def ssh_cmd(self) -> list[str]:
        return ["ssh", "-i", str(self.key), "-p", str(self.port),
                "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
                "-o", "LogLevel=ERROR", "-o", "ConnectTimeout=5", "-o", "BatchMode=yes",
                "root@127.0.0.1"]

    def sh(self, script: str, timeout: int = 120, input: str | None = None) -> tuple[int, str]:
        """Run a shell script as root in the machine; (status, output)."""
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


def stage_disk(st: Stage, image: str, disk: Path, env: dict) -> None:
    rc = run_logged([str(REPO / "scripts/rootless-disk.sh"), image, str(disk)],
                    st.log, env, timeout=3600)
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


def stage_desktop(st: Stage, machine: Path, outdir: Path, key: Path, gl: bool,
                  parts: set = frozenset({"session", "filter"})) -> None:
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

        greeter = wait_for(lambda: sh(
            "loginctl list-sessions --no-legend | grep -w greeter | grep -qw seat0")[0] == 0, 120)
        st.check("the login screen is up on seat0", greeter)
        time.sleep(8)  # the greeter's session exists before its window does
        vm.shoot("02-login-screen")

        vm.qmp.type(USER + "\n")
        time.sleep(3)
        vm.shoot("03-password")
        vm.qmp.type(USER_PASSWORD + "\n")

        props = wait_for(lambda: session_of(vm, USER), 120)
        vm.shoot("04-signed-in")
        log.write(f"# session: {props}\n")
        if not st.check("the account signs in through GDM", bool(props),
                        sh("loginctl list-sessions; journalctl -b -u gdm --no-pager | tail -30")[1]):
            raise StageError("no graphical session")
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
    finally:
        log.close()
        vm.stop()




# --- the suites, several machines at once ----------------------------------------

# How long each suite took on one machine (seconds, measured), so the groups
# come out even. A suite not listed counts as a minute.
SUITE_SECONDS = {"10-core": 15, "20-enforcement": 30, "30-apps": 55, "40-guest": 30,
                 "50-inspect": 30, "60-persistence": 110, "70-categories": 25,
                 "80-hardening": 50, "90-developer": 200}

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

CACHE = REPO / "build" / "pr-test" / "cache"
REGISTRY = "kosher-pr-test-registry"
REGISTRY_PORT = 5078
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


def switch_machine(st: Stage, machine: Path, workdir: Path, key: Path, tag: str,
                   digest: str) -> None:
    """Boot the machine, `bootc switch` it to the pushed image, reboot into
    it and check it is running that image: the update path a family's
    machine takes, from the set-up state the cached base holds."""
    vm = Machine(machine, workdir, key, gl=False, console="switch-console.log")
    try:
        if not st.check("the set-up machine boots and answers ssh", vm.wait_ssh()):
            raise StageError("no ssh")
        rc, out = vm.sh(SWITCH_SETUP + f"bootc switch 10.0.2.2:{REGISTRY_PORT}/kosher-linux:{tag}",
                        timeout=1800)
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
        rc, out = vm.sh(CLEAN_SWITCH)
        st.check("the harness registry is removed from the machine's trust again", rc == 0, out)
    finally:
        vm.stop()


def find_base(master: str) -> tuple[Path | None, str]:
    """A cached set-up base for master's commit, or a recent one."""
    bases = []
    for meta in CACHE.glob("base-*/meta.json"):
        try:
            m = json.loads(meta.read_text())
        except ValueError:
            continue
        if (meta.parent / "setup.qcow2").exists():
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


def make_base(st: Stage, master: str, sha: str, image: str, key: Path, env: dict) -> Path:
    """Master's image, installed and set up by the wizard, then switched to
    itself through the registry, so later switches pull only the layers a
    change makes. Once per master commit (or week)."""
    base_image = image if master == sha else "localhost/kosher-linux:pr-test-base"
    if base_image != image:
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
    registry_up(env, st.log)
    digest = push(base_image, "base", env, st.log)
    switch_machine(st, setup, base, key, "base", digest)
    (base / "meta.json").write_text(json.dumps(
        {"master": master, "image_id": stamp.read_text().strip(), "created": time.time()}))
    if base_image != image:
        subprocess.run(["podman", "rmi", "--ignore", base_image], capture_output=True)
    return base


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
    lines += ["| Stage | Result | Time | Checks |", "|---|---|---|---|"]
    for s in summary["stages"]:
        n = len(s["checks"])
        bad = sum(1 for c in s["checks"] if not c["ok"])
        lines.append(f"| {s['name']} | {s['status']} | {s['seconds'] / 60:.1f} min | "
                     f"{n - bad} of {n} passed |")
    timings = {k: v for s in summary["stages"] for k, v in (s.get("timings") or {}).items()}
    if timings:
        lines += ["", "Suites: " + ", ".join(f"{k} {v}s" for k, v in timings.items())]
    for s in summary["stages"]:
        failed = [c for c in s["checks"] if not c["ok"]]
        if failed or s["note"]:
            lines += ["", f"## {s['name']}: what failed", ""]
            if s["note"]:
                lines.append(f"- {s['note']}")
            lines += [f"- {c['name']}" for c in failed]
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


class Done(Exception):
    """Nothing more to run."""


def main() -> int:
    import threading

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
    parser.add_argument("--direct", action="store_true", default=bool(os.environ.get("CI")),
                        help="no cached base: a disk from the change's own image, set up over "
                             "ssh unless a fresh install is selected (the default in CI)")
    parser.add_argument("--fresh-base", action="store_true",
                        help="make a new cached base from master even if a recent one exists")
    parser.add_argument("--vms", type=int, default=int(os.environ.get("KOSHER_PR_TEST_VMS", "3")),
                        help="machines for the suites, besides the desktop's (default 3, "
                             "or KOSHER_PR_TEST_VMS)")
    parser.add_argument("--image", help="test this already-built image; skips the build")
    parser.add_argument("--keep", action="store_true",
                        help="keep the machine, the worktree and the image tag")
    parser.add_argument("--gl", action="store_true", default=bool(os.environ.get("KOSHER_GL")),
                        help="a virgl GPU for the desktop stage (KOSHER_GL=1); needs the host's QEMU")
    args = parser.parse_args()

    run_id = args.id or (f"pr-{args.target}" if args.target.isdigit()
                         else re.sub(r"[^A-Za-z0-9_.-]", "-", args.target))
    outdir = REPO / "build" / "pr-test" / run_id
    if outdir.exists():
        for child in outdir.iterdir():
            if child.name != "src":
                shutil.rmtree(child) if child.is_dir() else child.unlink()
    outdir.mkdir(parents=True, exist_ok=True)
    CACHE.mkdir(parents=True, exist_ok=True)
    env = sudo_trap(outdir)
    image = args.image or f"localhost/kosher-linux:pr-test-{run_id.lower()}"
    disk = outdir / "disk" / "disk.qcow2"
    machine = outdir / "machine.qcow2"
    key = CACHE / "id_ed25519"  # one key for the runs and the cached bases they boot
    if not key.exists():
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "pr-test",
                        "-f", str(key)], check=True)

    started = time.monotonic()
    summary = {"target": args.target, "id": run_id, "commit": "", "image": image,
               "image_id": "", "started": datetime.now(timezone.utc).isoformat(timespec="seconds"),
               "gl": args.gl, "stages": [], "result": "failed", "seconds": 0,
               "mode": "full" if args.full else ("direct" if args.direct else "fast")}
    stages: list[Stage] = []
    lock = threading.Lock()
    tree = REPO

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
            summary["commit"] = sha
            pre.check(f"the target is {sha[:9]}", True)
            changed, cf_diff, master = changes_against_master(sha, env, pre.log)
            sel = select(changed, cf_diff, everything=args.all, full=args.full)
            summary["selection"] = sel.as_dict()
            summary["changed"] = changed
            print("  what runs, and why:")
            for r in sel.reasons:
                print(f"    {r}")
        if pre.status != "passed" or sel is None or sel.empty:
            raise Done
        caps = {} if args.full else FAST_CAPS
        summary["caps"] = [f"{k}={v!r}" for k, v in caps.items()]

        if not args.image:
            with stage("build") as st:
                stage_build(st, tree, image, env)
            if not ok("build"):
                raise Done

        if sel.boot or args.direct:
            # The change's own image, installed: the path every run took before.
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
            base, why = (None, "") if args.fresh_base else find_base(master)
            if base is None:
                with stage("base") as st:
                    base = make_base(st, master, sha, image, key, env)
                if not ok("base"):
                    raise Done
                why = f"a new cached base for master {master[:9]}, set up by the boot test"
            summary["base"] = why
            print(f"  machine: {why}")
            child_overlay(base / "setup.qcow2", machine)
            meta = json.loads((base / "meta.json").read_text())
            target_id = subprocess.run(["podman", "image", "inspect", image, "--format", "{{.Id}}"],
                                       capture_output=True, text=True).stdout.strip()
            if target_id != meta.get("image_id"):
                with stage("switch") as st:
                    registry_up(env, st.log)
                    tag = f"pr-test-{run_id.lower()}"
                    digest = push(image, tag, env, st.log)
                    switch_machine(st, machine, outdir / "switch", key, tag, digest)
                if not ok("switch"):
                    raise Done
            prune_bases(base)

        if not machine.exists():
            raise Done

        # From here on, every machine is an overlay of the set-up one, all at once.
        workers = []
        if sel.desktop:
            def desktop():
                with stage("desktop") as st:
                    stage_desktop(st, child_overlay(machine, outdir / "desktop.qcow2"),
                                  outdir, key, args.gl, parts=sel.desktop)
            workers.append(threading.Thread(target=desktop))
        groups = groups_for(sel.ordered_suites(), machines_to_use(args.vms))
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
    except KeyboardInterrupt:
        print("\ninterrupted")
    finally:
        sudo_log = outdir / "sudo-attempts.log"
        if sudo_log.exists():
            s = Stage("no-sudo", outdir)
            s.check("nothing ran sudo", False, sudo_log.read_text())
            s.status = "failed"
            stages.append(s)
        summary["stages"] = [s.as_dict() for s in stages]
        ran = [s for s in stages if s.status != "skipped"]
        summary["result"] = ("passed" if ran and all(s.status == "passed" for s in ran)
                             else "failed")
        summary["seconds"] = round(time.monotonic() - started)
        write_report(outdir, summary)
        if not args.keep:
            for f in (machine, outdir / "desktop.qcow2"):
                f.unlink(missing_ok=True)
            shutil.rmtree(disk.parent, ignore_errors=True)
            if tree != REPO:
                subprocess.run(["git", "-C", str(REPO), "worktree", "remove", "--force", str(tree)],
                               capture_output=True)
            if not args.image:
                subprocess.run(["podman", "rmi", "--ignore", image], capture_output=True)
            for sock in ("qmp.sock", "vnc.sock"):
                (outdir / sock).unlink(missing_ok=True)

    print("\n" + (outdir / "report.md").read_text())
    return 0 if summary["result"] == "passed" else 1


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    sys.exit(main())
