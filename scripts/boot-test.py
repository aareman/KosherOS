#!/usr/bin/env python3
"""Boot the real disk image and prove it reaches first-boot setup.

Every other test we have exercises a system that is already running. Four
bugs shipped through that gap — a reinstall loop, a race with GDM, a
deadlock on plymouth-quit-wait, and a compositor with no seat — each living
between power-on and the wizard, and each "verified" by grepping the built
image, which proves a file is right and nothing about whether it boots.

This boots the actual disk headless and watches its serial console, which
carries systemd's own progress messages. That needs no sshd, no account and
no network, so it works on a disk installed from the shipped ISO as well as
a dev image — and the ORDER of those messages is exactly what the four
shipped bugs got wrong.

With no video device the wizard takes its text path and prompts on
/dev/console, so the test can also answer it over the same line. Work
happens on a throwaway overlay: the image handed in is never modified.

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
ADMIN_PASSWORD = "boot-test-pw-1"
BOOT_TIMEOUT = 420


class Console:
    """The machine's serial line: wait for text, type answers."""

    def __init__(self, sock: socket.socket, log_path: Path):
        self.sock = sock
        self.sock.setblocking(False)
        self.selector = selectors.DefaultSelector()
        self.selector.register(sock, selectors.EVENT_READ)
        self.text = ""
        log_path.parent.mkdir(parents=True, exist_ok=True)
        self.log = log_path.open("w")

    def expect(self, pattern: str, timeout: int) -> bool:
        regex = re.compile(pattern, re.IGNORECASE)
        deadline = time.monotonic() + timeout
        while True:
            if regex.search(self.text):
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            for _key, _mask in self.selector.select(min(remaining, 1.0)):
                chunk = self.sock.recv(65536)
                if not chunk:
                    return False
                decoded = chunk.decode("utf-8", "replace")
                self.text += decoded
                self.log.write(decoded)
                self.log.flush()

    def saw(self, pattern: str) -> bool:
        return re.search(pattern, self.text, re.IGNORECASE) is not None

    def position_of(self, pattern: str) -> int:
        match = re.search(pattern, self.text, re.IGNORECASE)
        return match.start() if match else -1

    def send(self, line: str) -> None:
        self.sock.sendall((line + "\n").encode())

    def tail(self, n: int = 700) -> str:
        # Console output is full of escape sequences; keep it readable.
        clean = re.sub(r"\x1b\[[0-9;?]*[a-zA-Z]", "", self.text)
        return clean[-n:]


def start_qemu(disk: Path, socket_path: Path) -> subprocess.Popen:
    return subprocess.Popen(
        ["qemu-system-x86_64", "-enable-kvm", "-cpu", "host",
         "-m", "3072", "-smp", "2",
         "-drive", f"file={disk},if=virtio",
         # No video device: the wizard takes its text path, which is what
         # makes this drivable over a serial line.
         "-vga", "none", "-display", "none",
         "-serial", f"unix:{socket_path},server=on,wait=off",
         "-netdev", "user,id=n0", "-device", "virtio-net-pci,netdev=n0"],
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
    raise TimeoutError(f"qemu never created {socket_path}")


class Results:
    def __init__(self):
        self.passed = self.failed = 0

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
    subprocess.run(["qemu-img", "create", "-q", "-f", "qcow2",
                    "-b", str(disk.resolve()), "-F", "qcow2", str(overlay)],
                   check=True)

    print(f"\nBooting {disk}  (overlay — the image is not modified)")
    qemu = start_qemu(overlay, workdir / "console.sock")
    console = None
    try:
        console = Console(connect(workdir / "console.sock"),
                          Path("build/boot-test-console.log"))

        print("\nFirst boot")
        reached = console.expect(r"kosher-firstboot|first-boot setup|Username:",
                                 BOOT_TIMEOUT)
        results.check("the first-boot wizard runs", reached, console.tail())

        # The race: GDM must not get there first.
        gdm_at = console.position_of(r"Started .*GNOME Display Manager")
        wizard_at = console.position_of(r"kosher-firstboot|first-boot setup")
        results.check(
            "GDM did not start before the wizard",
            wizard_at >= 0 and (gdm_at < 0 or wizard_at < gdm_at),
            f"wizard at {wizard_at}, gdm at {gdm_at}")

        # The deadlock: the boot got past plymouth-quit-wait.
        results.check(
            "the boot did not wedge on plymouth-quit-wait",
            not console.saw(r"job plymouth-quit-wait.*(running|timed out)"))

        # The missing seat: cage failing three times then giving up.
        results.check("the wizard did not exhaust its attempts",
                      not console.saw(r"giving up on the graphical wizard"))

        if console.expect(r"Username:", 180):
            results.check("setup asks for an administrator", True)
            console.send(ADMIN_USER)
            console.expect(r"Full name", 30)
            console.send("Boot Test")
            console.expect(r"Password:", 30)
            console.send(ADMIN_PASSWORD)
            console.expect(r"Confirm password:", 30)
            console.send(ADMIN_PASSWORD)
            created = console.expect(rf"Created {ADMIN_USER}", 120)
            results.check("the administrator account is created", created,
                          console.tail())
            console.expect(r"guardian password", 30)
            console.send("n")
            results.check("setup completes",
                          console.expect(r"Setup complete", 120), console.tail())
            results.check("the login screen starts afterwards",
                          console.expect(r"GNOME Display Manager|login:", 240),
                          console.tail())
        else:
            results.check("setup asks for an administrator", False, console.tail())
    except (TimeoutError, OSError) as e:
        results.check("the machine boots", False, str(e))
    finally:
        qemu.terminate()
        try:
            qemu.wait(timeout=20)
        except subprocess.TimeoutExpired:
            qemu.kill()

    print("\n" + "-" * 50)
    print(f"RESULT: {results.passed} passed, {results.failed} failed")
    print("console log: build/boot-test-console.log")
    shutil.rmtree(workdir, ignore_errors=True)
    return 1 if results.failed else 0


def main() -> int:
    disk = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_DISK
    if not disk.exists():
        print(f"No disk image at {disk}. Build one with: just vm", file=sys.stderr)
        return 2
    if not os.access("/dev/kvm", os.W_OK):
        print("/dev/kvm is not writable; this test needs KVM.", file=sys.stderr)
        return 2
    return run(disk)


if __name__ == "__main__":
    sys.exit(main())
