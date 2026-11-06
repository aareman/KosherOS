"""The first-boot wizard's unit, and what a kosherd restart does to it.

Seen on a fresh VM: `just deploy-kosherd` restarted the daemon an hour
after the wizard, and the desktop vanished. The wizard unit was still
"active" (RemainAfterExit) and Required the daemon, so the restart
stopped it; its stop hook started the tty1 getty, and GDM, which
conflicts with that getty, was stopped with the whole session. A daemon
crash on the first day would have done the same to a family.
"""

import re
from pathlib import Path

UNIT = (Path(__file__).parents[2]
        / "os-image/files/usr/lib/systemd/system/kosher-firstboot.service").read_text()


def _directives(section: str) -> list[tuple[str, str]]:
    body = UNIT.split(f"[{section}]", 1)[1].split("\n[", 1)[0]
    return re.findall(r"^([A-Za-z]+)=(.*)$", body, re.M)


def test_the_wizard_does_not_stay_active_after_it_has_run():
    assert ("Type", "oneshot") in _directives("Service")
    assert not any(k == "RemainAfterExit" for k, _ in _directives("Service"))


def test_a_daemon_restart_does_not_stop_the_wizard():
    unit = dict(_directives("Unit"))
    assert "kosherd.service" in unit.get("Wants", "")
    assert "kosherd.service" not in unit.get("Requires", "")
    assert "kosherd.service" in unit["After"]


def test_the_wizard_still_runs_before_the_login_screen_and_only_once():
    unit = dict(_directives("Unit"))
    before = " ".join(v for k, v in _directives("Unit") if k == "Before")
    assert "gdm.service" in before
    assert unit["ConditionPathExists"] == "!/var/lib/kosher/setup-complete"
    # The serial consoles are handed back after the wizard, on its exit —
    # and only those. getty.target pulls in the getty on tty1, which the
    # login screen conflicts with: a fresh machine ended its first boot at
    # a black text login once this hook ran at the wizard's exit.
    hooks = [v for k, v in _directives("Service") if k == "ExecStopPost"]
    assert len(hooks) == 1
    assert "getty.target" not in hooks[0] and "getty@tty1" not in hooks[0]
    assert "serial-getty@$c.service" in hooks[0] and "ttyS0" in hooks[0] and "hvc0" in hooks[0]
    assert '[ -e "/dev/$c" ]' in hooks[0], "only a console that exists; a missing one would fail its unit"


def test_a_serial_console_has_a_size_before_the_wizard_asks_anything():
    # With a 0x0 console, systemd asked the terminal for its size and read
    # the answer from the line the wizard was reading: the typed password
    # arrived empty and setup never finished (#177). Only a console with no
    # size is given one; a real screen is left as it is.
    pre = [v for k, v in _directives("Service") if k == "ExecStartPre"]
    assert len(pre) == 1
    assert 'stty -F /dev/console size' in pre[0] and '!= "0 0"' in pre[0]
    assert "stty -F /dev/console rows 24 cols 80" in pre[0]
    assert pre[0].startswith("-"), "a console stty cannot set must not stop setup"
    assert "$$(" in pre[0], "systemd's own $ must not eat the shell's"
