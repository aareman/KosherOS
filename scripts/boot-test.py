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


def _shell(console: Console, command: str, marker: str, timeout: int = 60) -> str:
    """Run one command in the logged-in shell; return what it printed."""
    start = len(console.text)
    console.send(f"{command}; echo {marker}=$?")
    console.expect(rf"{marker}=\d+", timeout)
    out = console.text[start:]
    # Drop the echoed command line and shell escape sequences.
    out = re.sub(r"\x1b\[[0-9;?]*[a-zA-Z]", "", out)          # CSI colours etc.
    out = re.sub(r"(\x1b)?\]3008;[^\\\x07]*(\\|\x07)", "", out)   # shell integration marks
    return out


def enforcement(console: Console, results: "Results") -> None:
    """Log in on the serial console as the new administrator and probe.

    Uses sites from the shipped catalogue rather than the tiny seed list,
    so this also proves the 5-million-domain database is present and
    consulted, not just that a hard-coded name is refused.
    """
    if not console.expect(r"kosheros login: ", 120):
        results.check("a console login is offered after setup", False, console.tail())
        return
    console.send(ADMIN_USER)
    console.expect(r"Password: ", 30)
    console.send(ADMIN_PASSWORD)
    if not console.expect(rf"{ADMIN_USER}@kosheros", 60):
        results.check("the administrator can log in", False, console.tail())
        return
    results.check("the administrator can log in", True)

    # No network in this VM (a firewalled CI runner, say) is not a filter
    # fault. Establish reachability first so a failure below means what it
    # says.
    probe = _shell(console, 'curl -s -m 20 -o /dev/null -w "code=%{http_code}" https://example.com/', "REACH")
    if "code=200" not in probe:
        print("  SKIP  enforcement checks: no internet from the VM")
        return

    issuer = _shell(console, "curl -sv -m 20 -o /dev/null https://example.com/ 2>&1 | grep -i issuer", "ISS")
    results.check("the administrator's HTTPS is inspected by the proxy",
                  "mitmproxy" in issuer, issuer[-400:])

    # gambling is in the default floor; unibet is classified only in the
    # full catalogue, not the seed.
    blocked = _shell(console, 'curl -s -m 20 -o /dev/null -w "code=%{http_code}" https://www.unibet.com/', "GAMB")
    results.check("a gambling site is blocked for the administrator by default",
                  "code=403" in blocked, blocked[-400:])
    dating = _shell(console, 'curl -s -m 20 -o /dev/null -w "code=%{http_code}" https://tinder.com/', "DATE")
    results.check("a dating site is blocked for the administrator by default",
                  "code=403" in dating, dating[-400:])
    page = _shell(console, "curl -s -m 20 https://www.unibet.com/ | grep -c 'This page is blocked'", "PAGE")
    results.check("a blocked site shows the block page, not an error",
                  re.search(r"^[1-9]\d*\s*$", page, re.M) is not None, page[-400:])


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

        # A getty on the same console reads the same keystrokes. When this
        # happened, the wizard printed every question and saw none of the
        # answers — the password went to a login prompt — and it surfaced
        # only as "the administrator account is created" failing, which
        # points nowhere near the cause.
        getty_at = console.position_of(r"Started .*serial-getty|Started .*getty@tty1")
        results.check(
            "no getty took the console from the wizard",
            getty_at < 0 or (wizard_at >= 0 and getty_at > wizard_at
                             and not console.saw(r"login: timed out")),
            f"wizard at {wizard_at}, getty at {getty_at}")

        # The machine should introduce itself by name. /etc/hostname is
        # bind-mounted during a container build, so writing it in a RUN
        # silently produced an empty file and every prompt said "fedora".
        results.check("the machine calls itself kosheros",
                      not console.saw(r"fedora login:"),
                      console.tail())

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
            # Now the check that matters most and was missing: log in as the
            # account the wizard just made and see whether its traffic is
            # actually filtered. The first family test found the wizard's
            # administrator in "filtered" mode with no categories at all —
            # every boot test before this one passed while gambling sites
            # loaded for the admin, because nothing here ever tried one.
            enforcement(console, results)
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


def refuse_stale_disk(disk: str) -> None:
    """Refuse to boot a disk that is not the current image, freshly built.

    `just test-boot` boots whatever qcow2 exists; it never rebuilds it.
    Three boots in a row tested a stale disk because nothing said so, and a
    later attempt to guard it was itself defeated by a stamp file written
    without a rebuild — a two-hour-old disk wearing a fresh stamp. So both
    things are checked here: the stamp names the current image, AND it was
    written together with the disk (one `just vm` run writes both seconds
    apart). Set KOSHER_BOOT_STALE_OK=1 to override.
    """
    import os
    import subprocess
    from pathlib import Path

    def short(image_id: str) -> str:
        return image_id.split(":")[-1][:12]

    current = subprocess.run(
        ["podman", "image", "inspect", "localhost/kosher-linux:dev",
         "--format", "{{.Id}}"], capture_output=True, text=True).stdout.strip()
    if not current:
        return  # no local image to compare against; not this harness's call
    current = short(current)

    stamp = Path(disk).parent / ".image-id"
    recorded = short(stamp.read_text().strip()) if stamp.exists() else ""
    try:
        skew = abs(os.path.getmtime(stamp) - os.path.getmtime(disk))
    except OSError:
        skew = 1e9

    ok = recorded == current and skew < 60
    if ok or os.environ.get("KOSHER_BOOT_STALE_OK") == "1":
        if not ok:
            print("WARNING: booting a stale disk because KOSHER_BOOT_STALE_OK=1")
        return

    print("REFUSING TO BOOT A STALE DISK")
    if recorded != current:
        print(f"  the disk was built from image {recorded or '(unstamped)'}, "
              f"but the current image is {current}.")
    else:
        print(f"  the stamp names the current image but was written "
              f"{skew/60:.0f} min from the disk — the disk was not rebuilt "
              "with it.")
    print("  Booting it would test old code and report it as current.")
    print("  Rebuild the disk:   just vm")
    raise SystemExit(2)


def main() -> int:
    disk = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_DISK
    refuse_stale_disk(str(disk))
    if not disk.exists():
        print(f"No disk image at {disk}. Build one with: just vm", file=sys.stderr)
        return 2
    if not os.access("/dev/kvm", os.W_OK):
        print("/dev/kvm is not writable; this test needs KVM.", file=sys.stderr)
        return 2
    return run(disk)


if __name__ == "__main__":
    sys.exit(main())
