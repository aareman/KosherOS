"""The installer answers the disk question itself when it can.

Anaconda's hub asked the person to find the disk even when the laptop has
exactly one. A %pre script in the ISO's kickstart now picks that disk and
writes the partitioning for it; with two or more candidate disks it writes
nothing and Anaconda asks, so a second drive is never wiped unseen. The
installer itself still waits for the person to press Begin Installation.
"""

import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
CONFIG = ROOT / "os-image/iso-config.toml"


@pytest.fixture(scope="module")
def kickstart() -> str:
    return tomllib.loads(CONFIG.read_text())["customizations"]["installer"]["kickstart"]["contents"]


def _pre_script(kickstart: str) -> str:
    body = kickstart.split("%pre", 1)[1].split("%end", 1)[0]
    return "#!/bin/bash\n" + body.split("\n", 1)[1]


def test_the_kickstart_still_answers_language_keyboard_and_time(kickstart):
    for line in ("lang en_US.UTF-8", "keyboard us", "timezone America/New_York --utc",
                 "reboot --eject"):
        assert line in kickstart


def test_the_disk_is_chosen_in_pre_and_included(kickstart):
    assert "%pre --interpreter=/bin/bash" in kickstart
    assert "%include /tmp/kosher-disk.ks" in kickstart
    script = _pre_script(kickstart)
    assert "ignoredisk --only-use=$disk" in script
    assert "clearpart --all --initlabel --disklabel=gpt --drives=$disk" in script
    assert "autopart" in script
    # Never the stick the installer booted from, never a removable drive,
    # and only when there is exactly one.
    assert "/run/install/repo" in script
    assert 'removable' in script
    assert '"${#candidates[@]}" -eq 1' in script


def test_no_disk_is_wiped_without_the_pre_script_choosing_it(kickstart):
    # Outside the %pre block there must be no partitioning at all: that is
    # what keeps a two-disk machine on the interactive path.
    outside = kickstart.split("%pre", 1)[0] + kickstart.split("%end", 1)[1]
    for word in ("clearpart", "autopart", "zerombr", "ignoredisk"):
        assert word not in outside, word


def test_the_install_still_waits_for_a_person(kickstart):
    # Non-interactive would start erasing the moment the stick boots.
    assert "--non-interactive" not in kickstart


@pytest.mark.skipif(shutil.which("shellcheck") is None, reason="shellcheck not installed")
def test_the_pre_script_passes_shellcheck(kickstart, tmp_path):
    script = tmp_path / "pre.sh"
    script.write_text(_pre_script(kickstart))
    result = subprocess.run(["shellcheck", "-s", "bash", str(script)],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stdout


def test_the_pre_script_chooses_one_disk_and_declines_two(kickstart, tmp_path):
    """Run the real script against a fake /sys/block."""
    if shutil.which("bash") is None:
        pytest.skip("no bash")
    script = _pre_script(kickstart)
    # Point it at a scratch sysfs and stub the two tools it calls.
    script = script.replace("/sys/block/*", str(tmp_path / "block") + "/*")
    script = script.replace("out=/tmp/kosher-disk.ks", f"out={tmp_path}/disk.ks")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "findmnt").write_text("#!/bin/sh\necho /dev/sdb1\n")
    (bin_dir / "lsblk").write_text("#!/bin/sh\necho sdb\n")
    for tool in ("findmnt", "lsblk"):
        (bin_dir / tool).chmod(0o755)

    def disk(name: str, removable: str, sectors: int) -> None:
        d = tmp_path / "block" / name
        d.mkdir(parents=True)
        (d / "removable").write_text(removable + "\n")
        (d / "size").write_text(f"{sectors}\n")

    disk("sda", "0", 500_000_000)      # the internal drive
    disk("sdb", "1", 60_000_000)       # the installer stick (also removable)
    disk("loop0", "0", 8_000_000)      # never a target
    env = {"PATH": f"{bin_dir}:/usr/bin:/bin"}
    run = subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True)
    assert run.returncode == 0, run.stderr
    chosen = (tmp_path / "disk.ks").read_text()
    assert "ignoredisk --only-use=sda" in chosen
    assert "--drives=sda" in chosen

    disk("nvme0n1", "0", 1_000_000_000)   # a second internal drive: ask
    run = subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True)
    assert run.returncode == 0, run.stderr
    assert (tmp_path / "disk.ks").read_text() == ""
    assert "Anaconda will ask" in run.stdout
