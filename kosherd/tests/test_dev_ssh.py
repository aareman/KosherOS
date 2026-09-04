"""Dev disks must be reachable as root; production disks must not run sshd.

Both from ONE image. The build used to `systemctl disable sshd` and the dev
disk config asked bootc-image-builder to enable it again, which it never
did — so dev disks had no ssh on any socket, and when a filtering fault had
to be diagnosed there was no way to read the proxy's journal. Now sshd is
enabled but conditional on /root/.ssh/authorized_keys, which only the dev
config injects.
"""

from pathlib import Path

ROOT = Path(__file__).parents[2]
CONTAINERFILE = (ROOT / "os-image/Containerfile").read_text()
DROPIN = ROOT / "os-image/files/usr/lib/systemd/system/sshd.service.d/kosher-dev-only.conf"
DEV_CONFIG = (ROOT / "os-image/dev-config.toml").read_text()


def test_sshd_is_enabled_in_the_image_but_gated_on_a_root_key():
    assert "systemctl disable sshd" not in CONTAINERFILE
    assert "systemctl enable sshd.service" in CONTAINERFILE
    text = DROPIN.read_text()
    assert "ConditionPathExists=/root/.ssh/authorized_keys" in text


def test_the_dev_disk_injects_a_root_key_and_relies_on_nothing_else():
    # The user block is what bootc-image-builder honours; a services block
    # it silently does not, so its absence here is deliberate.
    assert 'name = "root"' in DEV_CONFIG and "@SSH_KEY@" in DEV_CONFIG
    assert "[customizations.services]" not in DEV_CONFIG


def test_production_installers_ship_no_root_key():
    iso = (ROOT / "os-image/iso-config.toml").read_text()
    assert "authorized_keys" not in iso and "@SSH_KEY@" not in iso
    assert 'name = "root"' not in iso
