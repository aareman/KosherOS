"""The advanced desktop always gets its shell (kosher-shell.service).

An account with Umbriel's example config in ~/.config/umbriel/config.toml,
autostart commented out, signed in to a black screen: Umbriel ran, Noctalia
never started. kosher-shell starts Noctalia when the compositor has not.
The script runs here with stand-ins for pgrep, sleep and noctalia.
"""

import os
import subprocess
from pathlib import Path

REPO = Path(__file__).parents[2]
SCRIPT = REPO / "os-image/files/usr/libexec/kosher-shell"
UNIT = REPO / "os-image/files/usr/lib/systemd/user/kosher-shell.service"
CONTAINERFILE = REPO / "os-image/Containerfile"


def _run(tmp_path: Path, desktop: str, running: bool):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    started = tmp_path / "started"
    stubs = {
        "pgrep": "exit 0" if running else "exit 1",
        "sleep": "exit 0",
        "noctalia": f"touch {started}",
    }
    for name, body in stubs.items():
        stub = bin_dir / name
        stub.write_text(f"#!/bin/sh\n{body}\n")
        stub.chmod(0o755)
    env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "XDG_CURRENT_DESKTOP": desktop}
    res = subprocess.run(["sh", str(SCRIPT)], env=env, capture_output=True,
                         text=True, timeout=30)
    return res, started.exists()


def test_starts_noctalia_when_the_compositor_did_not(tmp_path):
    res, started = _run(tmp_path, "umbriel", running=False)
    assert res.returncode == 0
    assert started
    assert "starting Noctalia" in res.stderr


def test_leaves_a_running_shell_alone(tmp_path):
    res, started = _run(tmp_path, "umbriel", running=True)
    assert res.returncode == 0
    assert not started


def test_each_advanced_compositor_is_covered(tmp_path):
    for desktop in ("niri", "Hyprland", "umbriel"):
        sub = tmp_path / desktop
        sub.mkdir()
        _, started = _run(sub, desktop, running=False)
        assert started, desktop


def test_gnome_is_left_alone(tmp_path):
    res, started = _run(tmp_path, "GNOME", running=False)
    assert res.returncode == 0
    assert not started


def test_the_unit_runs_with_every_graphical_session():
    unit = UNIT.read_text()
    assert "ExecStart=/usr/libexec/kosher-shell" in unit
    assert "WantedBy=graphical-session.target" in unit
    assert "PartOf=graphical-session.target" in unit
    assert "systemctl --global enable kosher-shell.service" in CONTAINERFILE.read_text()
    assert os.access(SCRIPT, os.X_OK)
