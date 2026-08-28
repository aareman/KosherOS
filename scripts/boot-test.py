#!/usr/bin/env python3
"""Boot the real disk image and prove it reaches first-boot setup.

Every other test we have exercises a system that is already running. Four
bugs shipped through that gap — a reinstall loop, a race with GDM, a
deadlock on plymouth-quit-wait, and a compositor with no seat — each of
which only appeared between power-on and the wizard, and each of which was
"verified" by grepping the built image.

This drives the machine the way a person does: boot it, watch the console,
type the answers, and check what happens next. It runs headless with no
video device, so the wizard takes its text path and the whole exchange
happens over the serial line.

Work is done on a temporary overlay, so the disk image handed to it is
never modified and the test can be run repeatedly.

    scripts/boot-test.py [disk.qcow2]
"""

from __future__ import annotations

import os
import re
import selectors
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

DEFAULT_DISK = Path("build/qcow2/disk.qcow2")
ADMIN_USER = "boottest"
ADMIN_PASSWORD = "boot-test-pw"
BOOT_TIMEOUT = 300


class Console:
    """The machine's serial console: read patterns, type answers."""

    def __init__(self, sock: socket.socket, log: Path):
        self.sock = sock
        self.sock.setblocking(False)
        self.selector = selectors.DefaultSelector()
        self.selector.register(sock, selectors.EVENT_READ)
        self.buffer = ""
        self.log = log.open("w")

    def expect(self, pattern: str, timeout: int = 120) -> str:
        """Wait for a regex, returning everything up to and including it."""
        deadline = time.monotonic() + timeout
        regex = re.compile(pattern)
        while True:
            if regex.search(self.buffer):
                return self.buffer
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                tail = self.buffer[-1500:]
                raise TimeoutError(
                    f"timed out after {timeout}s waiting for {pattern!r}\n"
                    f"--- last console output ---\n{tail}")
            for _key, _mask in self.selector.select(min(remaining, 1.0)):
                chunk = self.sock.recv(65536)
                if not chunk:
                    raise EOFError("the machine closed its console")
                text = chunk.decode("utf-8", "replace")
                self.buffer += text
                self.log.write(text)
                self.log.flush()

    def send(self, text: str) -> None:
        self.sock.sendall((text + "\n").encode())

    def forget(self) -> None:
        """Drop what has been read, so the next expect() looks forward."""
        self.buffer = ""


def boot(disk: Path, socket_path: Path, extra_args: list[str] | None = None):
    """Start QEMU headless with its serial line on a unix socket."""
    command = [
        "qemu-system-x86_64", "-enable-kvm", "-cpu", "host", "-m", "3072",
        "-smp", "2", "-drive", f"file={disk},if=virtio",
        # No video device at all: this is what makes the wizard take its
        # text path, and it is also how a serial install behaves.
        "-nographic", "-vga", "none",
        "-serial", f"unix:{socket_path},server=on,wait=off",
        "-netdev", "user,id=n0", "-device", "virtio-net-pci,netdev=n0",
        "-display", "none",
    ]
    return subprocess.Popen(command + (extra_args or []),
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def connect(socket_path: Path, timeout: int = 30) -> socket.socket:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.connect(str(socket_path))
            return sock
        except (FileNotFoundError, ConnectionRefusedError):
            time.sleep(0.5)
    raise TimeoutError(f"QEMU never created {socket_path}")


class Results:
    def __init__(self):
        self.passed = 0
        self.failed = 0

    def check(self, description: str, ok: bool, detail: str = "") -> None:
        if ok:
            self.passed += 1
            print(f"  PASS  {description}")
        else:
            self.failed += 1
            print(f"  FAIL  {description}")
            if detail:
                print("        " + detail.strip().replace("\n", "\n        "))


def run(disk: Path) -> int:
    results = Results()
    workdir = Path(tempfile.mkdtemp(prefix="kosher-boot-test-"))
    overlay = workdir / "overlay.qcow2"
    socket_path = workdir / "console.sock"
    log_path = Path("build/boot-test-console.log")
    log_path.parent.mkdir(parents=True, exist_ok=True)

    # Never touch the image we were given.
    subprocess.run(
        ["qemu-img", "create", "-q", "-f", "qcow2", "-b", str(disk.resolve()),
         "-F", "qcow2", str(overlay)], check=True)

    print(f"\nBooting {disk} (overlay; the image is not modified)")
    qemu = boot(overlay, socket_path)
    try:
        console = Console(connect(socket_path), log_path)

        print("\nFirst boot")
        console.expect(r"kosher-firstboot|KosherOS first-boot|Starting setup",
                       timeout=BOOT_TIMEOUT)
        results.check("the first-boot wizard runs", True)

        # The bugs this catches: GDM winning the race, or the boot wedging
        # on plymouth-quit-wait before the wizard is ever reached.
        before_wizard = console.buffer
        results.check("GDM did not start before the wizard",
                      "Starting GNOME Display Manager" not in before_wizard,
                      before_wizard[-600:])
        results.check("boot did not wedge on plymouth-quit-wait",
                      "job plymouth-quit-wait" not in before_wizard.lower())

        console.expect(r"Username:", timeout=120)
        results.check("setup asks for an administrator account", True)
        console.forget()
        console.send(ADMIN_USER)
        console.expect(r"Full name")
        console.send("Boot Test")
        console.expect(r"Password:")
        console.send(ADMIN_PASSWORD)
        console.expect(r"Confirm password:")
        console.send(ADMIN_PASSWORD)
        console.expect(r"Created " + ADMIN_USER, timeout=90)
        results.check("the administrator account is created", True)

        console.expect(r"guardian password")
        console.send("n")
        console.expect(r"Setup complete", timeout=90)
        results.check("setup completes", True)

        console.forget()
        console.expect(r"Starting GNOME Display Manager|gdm\.service", timeout=180)
        results.check("the login screen starts after setup", True)

    except (TimeoutError, EOFError) as e:
        results.check("boot reached first-boot setup", False, str(e))
    finally:
        qemu.terminate()
        try:
            qemu.wait(timeout=20)
        except subprocess.TimeoutExpired:
            qemu.kill()

    # Second boot: setup must not run again.
    print("\nSecond boot")
    socket_path2 = workdir / "console2.sock"
    qemu = boot(overlay, socket_path2)
    try:
        console = Console(connect(socket_path2), Path("build/boot-test-console2.log"))
        console.expect(r"Starting GNOME Display Manager|gdm\.service|login:",
                       timeout=BOOT_TIMEOUT)
        results.check("the login screen starts", True)
        results.check("setup does not run a second time",
                      "Username:" not in console.buffer)
    except (TimeoutError, EOFError) as e:
        results.check("second boot reaches the login screen", False, str(e))
    finally:
        qemu.terminate()
        try:
            qemu.wait(timeout=20)
        except subprocess.TimeoutExpired:
            qemu.kill()
        shutil.rmtree(workdir, ignore_errors=True)

    print("\n" + "-" * 50)
    print(f"RESULT: {results.passed} passed, {results.failed} failed")
    print(f"console log: {log_path}")
    return 1 if results.failed else 0


def main() -> int:
    disk = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_DISK
    if not disk.exists():
        print(f"No disk image at {disk}.", file=sys.stderr)
        print("Build one first:  just vm     (or pass a path)", file=sys.stderr)
        return 2
    if not os.access("/dev/kvm", os.W_OK):
        print("/dev/kvm is not writable; this test needs KVM.", file=sys.stderr)
        return 2
    return run(disk)


if __name__ == "__main__":
    sys.exit(main())
