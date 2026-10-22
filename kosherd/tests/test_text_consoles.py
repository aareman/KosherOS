"""Who a text console lets in, and what the serial line needs.

F8 in #33 (#186): every getty offered a shell to every account, outside
the desktop and so outside its app list, lock screen and time warnings.
The serial line needs two things of its own: the boot splash taken down
on a machine with no display, since a plymouthd left running reads the
console, and a size, or systemd asks the terminal for one and reads the
answer from the same line (#177 was the wizard's version). The password
the boot test lost at the serial login (#180) was the test's own doing;
see test_boot_test_console.py.
"""

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).parents[2]
CONTAINERFILE = (ROOT / "os-image/Containerfile").read_text()
GETTY_DROPIN = (ROOT / "os-image/files/usr/lib/systemd/system/"
                "serial-getty@.service.d/kosher-size.conf").read_text()
SETUP_SESSION = (ROOT / "os-image/files/usr/bin/kosher-setup-session").read_text()


def _login_sed() -> str:
    match = re.search(r"sed -i '(0,/\^account/\{[^']*ingroup kosher-admin[^']*)' /etc/pam\.d/login",
                      CONTAINERFILE)
    assert match, "the image does not limit console login to administrators"
    return match.group(1)


FEDORA_LOGIN = """#%PAM-1.0
auth       substack     system-auth
auth       include      postlogin
account    [success=1 default=bad] pam_time.so
account    optional pam_exec.so quiet /usr/bin/kosher-time-refused
account    required     pam_nologin.so
account    include      system-auth
session    include      system-auth
"""


def _apply(sed: str, text: str) -> list[str]:
    out = subprocess.run(["sed", sed], input=text, capture_output=True, text=True, check=True)
    return out.stdout.splitlines()


def test_a_console_admits_administrators_and_root_and_nobody_else():
    lines = _apply(_login_sed(), FEDORA_LOGIN)
    account = [line.split(None, 1)[1] for line in lines if line.startswith("account")]
    # Root skips both checks; an administrator skips the refusal; anyone
    # else is refused when there is a terminal. All three come before
    # anything else in the account stack can let an account through.
    assert account[0] == "[success=2 default=ignore] pam_succeed_if.so quiet uid eq 0"
    assert account[1] == "[success=1 default=ignore] pam_succeed_if.so quiet user ingroup kosher-admin"
    assert account[2] == "required     pam_succeed_if.so quiet tty !~ ?*"
    # The time limits are still checked after it.
    assert "pam_time.so" in account[3]
    # Nothing else changed.
    assert [line for line in lines if "pam_succeed_if" not in line] == FEDORA_LOGIN.splitlines()


def test_a_session_without_a_terminal_is_not_a_console():
    # systemd-run with PAMName=login opens a session through the same
    # service with PAM_TTY empty; the VM suites rely on it for a seat-less
    # session of a supervised account, and it offers no shell to anyone.
    # `tty !~ ?*` is met only by an empty tty, so that path still passes.
    lines = _apply(_login_sed(), FEDORA_LOGIN)
    refusal = next(line for line in lines if "tty !~" in line)
    assert refusal.split()[1] == "required"
    assert refusal.rstrip().endswith("tty !~ ?*")


def test_the_login_screen_is_not_limited_this_way():
    # Supervised accounts sign in at the login screen, which has its own
    # PAM services; only `login`, the gettys', is limited.
    assert "/etc/pam.d/gdm-password" not in _login_sed()
    assert re.search(r"ingroup kosher-admin[^\n]*/etc/pam\.d/login", CONTAINERFILE)


def test_a_serial_line_has_a_size_so_nobody_asks_it():
    service = GETTY_DROPIN.split("[Service]", 1)[1]
    directives = dict(re.findall(r"^([A-Za-z]+)=(.*)$", service, re.M))
    assert directives.get("TTYRows") == "24"
    assert directives.get("TTYColumns") == "80"


def test_the_text_wizard_takes_the_boot_splash_down_first():
    # With no display, nothing else ever quits Plymouth (GDM does once its
    # greeter is on a screen), and a plymouthd left running reads the
    # console and replays the boot log over it. Seen as the serial login's
    # password going missing (#180). The text path has to quit it before
    # the wizard talks to the console, as the graphical path does for the
    # compositor, and finish the job if quitting hangs.
    text_path = SETUP_SESSION.split('if [ "$want_text" = yes ]; then', 1)[1]
    text_path = text_path.split("kosherctl setup", 1)[0]
    assert "timeout 10s plymouth quit" in text_path
    assert "pkill -x plymouthd" in text_path
