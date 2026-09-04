"""The finish step of first-boot setup, against a read-only /boot.

On ostree /boot is mounted read-only. The first person to enable the boot
menu password on a real machine got "cannot write" and a wizard they could
not complete. The write must remount for the duration and put it back.
"""

import errno
import subprocess
from pathlib import Path

import pytest

from kosherd import daemon as d
from kosherd.policy import PolicyError


@pytest.fixture
def user_cfg(tmp_path, monkeypatch):
    boot = tmp_path / "boot"
    (boot / "grub2").mkdir(parents=True)
    monkeypatch.setattr(d, "GRUB_USER_CFG", boot / "grub2" / "user.cfg")
    return boot / "grub2" / "user.cfg"


def fake_mkpasswd(*args, **kwargs):
    return subprocess.CompletedProcess(
        args, 0, stdout="PBKDF2 hash of your password is grub.pbkdf2.sha512.10000.AB.CD\n",
        stderr="")


def test_the_boot_password_is_written_where_grub_reads_it(user_cfg, monkeypatch):
    calls = []

    def run(cmd, *a, **k):
        calls.append(cmd)
        if cmd[0] == "grub2-mkpasswd-pbkdf2":
            return fake_mkpasswd(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(d.subprocess, "run", run)
    d.Daemon._set_grub_password("boot-pass-1")
    assert user_cfg.read_text() == "GRUB2_PASSWORD=grub.pbkdf2.sha512.10000.AB.CD\n"
    assert (user_cfg.stat().st_mode & 0o777) == 0o600
    # A writable /boot needs no remount, and none was attempted.
    assert not any(c[0] == "mount" for c in calls)


def test_a_read_only_boot_is_remounted_for_the_write_and_restored(user_cfg, monkeypatch):
    calls = []
    state = {"ro": True}

    def run(cmd, *a, **k):
        calls.append(cmd)
        if cmd[0] == "grub2-mkpasswd-pbkdf2":
            return fake_mkpasswd(cmd)
        if cmd[:3] == ["mount", "-o", "remount,rw"]:
            state["ro"] = False
        if cmd[:3] == ["mount", "-o", "remount,ro"]:
            state["ro"] = True
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    real_write = Path.write_text

    def write_text(self, text, *a, **k):
        if self == user_cfg and state["ro"]:
            raise OSError(errno.EROFS, "Read-only file system")
        return real_write(self, text, *a, **k)

    monkeypatch.setattr(d.subprocess, "run", run)
    monkeypatch.setattr(Path, "write_text", write_text)
    d.Daemon._set_grub_password("boot-pass-1")

    assert user_cfg.exists(), "the write succeeded after the remount"
    mounts = [c for c in calls if c[0] == "mount"]
    assert mounts[0][2] == "remount,rw" and mounts[-1][2] == "remount,ro"
    assert state["ro"], "/boot is read-only again afterwards"
    assert str(mounts[0][3]) == str(user_cfg.parent.parent), "remounts /boot, not /boot/grub2"


def test_a_boot_that_cannot_be_made_writable_is_a_clear_error(user_cfg, monkeypatch):
    def run(cmd, *a, **k):
        if cmd[0] == "grub2-mkpasswd-pbkdf2":
            return fake_mkpasswd(cmd)
        return subprocess.CompletedProcess(cmd, 32, stdout="",
                                           stderr="mount: /boot: permission denied")

    def write_text(self, text, *a, **k):
        raise OSError(errno.EROFS, "Read-only file system")

    monkeypatch.setattr(d.subprocess, "run", run)
    monkeypatch.setattr(Path, "write_text", write_text)
    with pytest.raises(PolicyError, match="could not make .* writable"):
        d.Daemon._set_grub_password("boot-pass-1")


def test_the_dbus_interface_offers_admin_exists():
    assert 'name="AdminExists"' in d.INTROSPECTION_XML
