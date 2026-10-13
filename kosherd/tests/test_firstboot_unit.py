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
    # The console is still handed back after the wizard, on its exit.
    assert any(k == "ExecStopPost" and "getty.target" in v for k, v in _directives("Service"))
