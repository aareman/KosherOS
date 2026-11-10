"""Exercise recovery against a real LUKS2 header in a disposable regular file.

No loop device, host mount or root access is needed. This proves keyslot
operations; the installer/boot/recovery VM remains a separate acceptance test.
"""

import shutil
import subprocess

import pytest

from kosherd import disksecurity as disk


@pytest.mark.skipif(shutil.which("cryptsetup") is None, reason="cryptsetup is unavailable")
def test_a_real_luks2_volume_accepts_the_saved_key_and_keeps_the_original(tmp_path, monkeypatch):
    volume = tmp_path / "volume.img"
    with volume.open("wb") as stream:
        stream.truncate(64 * 1024 * 1024)
    password = "only this disposable test image"
    with disk._key_fd(password) as fd:
        subprocess.run(["cryptsetup", "luksFormat", "--type", "luks2", "--batch-mode",
                        "--pbkdf", "pbkdf2", "--pbkdf-force-iterations", "1000",
                        "--key-file", f"/proc/self/fd/{fd}", str(volume)],
                       check=True, capture_output=True, pass_fds=(fd,))
    monkeypatch.setattr(disk, "STATE", tmp_path / "state" / "recovery.json")
    monkeypatch.setattr(disk, "_backing", lambda path: {"test-volume": str(volume)})
    real_run = disk._run

    def quick_test_key(argv, **kwargs):
        # Testing keyslot lifecycle, not the machine's PBKDF benchmark.
        if "luksAddKey" in argv:
            argv = [*argv[:2], "--pbkdf", "pbkdf2", "--pbkdf-force-iterations", "1000", *argv[2:]]
        return real_run(argv, **kwargs)

    monkeypatch.setattr(disk, "_run", quick_test_key)
    key = disk.prepare(password)
    disk._verify(str(volume), password, 0)
    disk.confirm(key)
    disk.require_ready()
    with pytest.raises(disk.DiskError):
        disk._verify(str(volume), "incorrect key")
    assert key not in disk.STATE.read_text()
    assert password not in disk.STATE.read_text()
