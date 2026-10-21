"""Who a text console lets in, and the serial line's size.

F8 in #33 (#186): every getty offered a shell to every account, outside
the desktop and so outside its app list, lock screen and time warnings.
The serial line also lost the password typed at its login prompt (#180),
for the same reason the first-boot wizard once lost its answers (#177).
"""

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).parents[2]
CONTAINERFILE = (ROOT / "os-image/Containerfile").read_text()
GETTY_DROPIN = (ROOT / "os-image/files/usr/lib/systemd/system/"
                "serial-getty@.service.d/kosher-size.conf").read_text()


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


def test_console_login_is_for_administrators_and_root():
    lines = _apply(_login_sed(), FEDORA_LOGIN)
    account = [line.split(None, 1)[1] for line in lines if line.startswith("account")]
    # Root first, skipping the group check; then the group check, before
    # anything else in the account stack can let an account through.
    assert account[0] == "[success=1 default=ignore] pam_succeed_if.so quiet uid eq 0"
    assert account[1] == "required     pam_succeed_if.so quiet user ingroup kosher-admin"
    # The time limits are still checked after it.
    assert "pam_time.so" in account[2]
    # Nothing else changed.
    assert [line for line in lines if "pam_succeed_if" not in line] == FEDORA_LOGIN.splitlines()


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
