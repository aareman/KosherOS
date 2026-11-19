"""Disk recovery must prove an independent unlock and never expose a password."""

import json
import hashlib
import os
import subprocess

import pytest

from kosherd import disksecurity as disk


def test_only_the_backing_luks_device_counts():
    tree = {"blockdevices": [{"path": "/dev/mapper/root", "type": "crypt", "children": [
        {"path": "/dev/vda3", "fstype": "crypto_LUKS", "uuid": "system"},
    ]}]}
    assert disk.luks_ancestors(tree) == {"system": "/dev/vda3"}
    assert disk.luks_ancestors({"blockdevices": [
        {"path": "/dev/vda3", "fstype": "ext4", "uuid": "plain"}]}) == {}


def test_mixed_encrypted_and_plain_raid_members_are_not_protected():
    tree = {"blockdevices": [{"path": "/dev/md0", "type": "raid1", "children": [
        {"path": "/dev/vda3", "fstype": "crypto_LUKS", "uuid": "encrypted"},
        {"path": "/dev/vdb3", "fstype": None, "uuid": None},
    ]}]}
    assert disk.luks_ancestors(tree) == {}


def test_growth_understands_only_the_plain_encrypted_ext4_layout():
    tree = {"blockdevices": [{"path": "/dev/mapper/root", "type": "crypt", "fstype": "ext4",
        "children": [{"path": "/dev/nvme0n1p3", "type": "part", "children": [
            {"path": "/dev/nvme0n1", "type": "disk"}]}]}]}
    assert disk.encrypted_root_layout(tree) == ("/dev/mapper/root", "/dev/nvme0n1p3", "/dev/nvme0n1")
    tree["blockdevices"][0]["type"] = "lvm"
    assert disk.encrypted_root_layout(tree) is None
    tree["blockdevices"][0]["type"] = "crypt"
    tree["blockdevices"][0]["fstype"] = "xfs"
    assert disk.encrypted_root_layout(tree) is None


def test_recovery_setup_calls_are_closed_permanently_after_first_boot():
    from kosherd import access

    for method in ("DiskSecurity", "PrepareDiskRecovery", "ConfirmDiskRecovery"):
        req = access.evaluate(method, setup_complete=True, session_unlocked=True,
                              guardian_enabled=False, guardian_proven=False)
        assert not req.allowed
    for method in ("PrepareDiskRecovery", "ConfirmDiskRecovery"):
        assert method in access.NO_ARGS_LOGGED


def test_the_daemon_cannot_finish_setup_before_recovery_is_verified(monkeypatch, tmp_path):
    from kosherd import daemon
    from kosherd.policy import Policy, PolicyError, UserPolicy

    instance = daemon.Daemon.__new__(daemon.Daemon)
    instance.policy = Policy(users=[UserPolicy(uid=1000, username="parent", mode="filtered", admin=True)])
    stamp = tmp_path / "setup-complete"
    monkeypatch.setattr(daemon, "SETUP_STAMP", stamp)
    monkeypatch.setattr(disk, "require_ready", lambda: (_ for _ in ()).throw(
        disk.DiskError("Save and verify the disk recovery key")))
    with pytest.raises(PolicyError, match="Save and verify"):
        instance.impl_FinishSetup("", "")
    assert not stamp.exists()


@pytest.fixture
def backend(tmp_path, monkeypatch):
    monkeypatch.setattr(disk, "STATE", tmp_path / "state" / "recovery.json")
    monkeypatch.setattr(disk, "_backing", lambda path: {"system": "/dev/vda3"})
    state = {"slots": {"0": "original disk password"}, "calls": []}

    def run(argv, **kwargs):
        state["calls"].append(argv)
        stdout = ""
        if "luksDump" in argv:
            stdout = json.dumps({"keyslots": {k: {"salt": hashlib.sha256(v.encode()).hexdigest()}
                                               for k, v in state["slots"].items()}})
        elif "--test-passphrase" in argv:
            fd = int(argv[argv.index("--key-file") + 1].rsplit("/", 1)[1])
            key = os.read(fd, 4096).decode()
            slots = state["slots"]
            if "--key-slot" in argv:
                slot = argv[argv.index("--key-slot") + 1]
                slots = {slot: slots.get(slot)}
            if key not in slots.values():
                raise disk.DiskError("wrong password")
        elif "luksAddKey" in argv:
            slot = argv[argv.index("--key-slot") + 1]
            fd = int(argv[-1].rsplit("/", 1)[1])
            assert slot not in state["slots"]
            state["slots"][slot] = os.read(fd, 4096).decode()
        elif "luksKillSlot" in argv:
            del state["slots"][argv[-1]]
        else:
            raise AssertionError(argv)
        return subprocess.CompletedProcess(argv, 0, stdout, "")

    monkeypatch.setattr(disk, "_run", run)
    return state


@pytest.mark.parametrize("plain_path", ["/var", "/etc"])
def test_plain_policy_or_state_is_not_reported_as_protected(backend, monkeypatch, plain_path):
    monkeypatch.setattr(disk, "_backing", lambda path: {} if path == plain_path else {"system": "/dev/vda3"})
    result = disk.inspect()
    assert result["encrypted"] is False
    assert result["supported"] is False


def test_separate_encrypted_disks_are_not_given_one_recovery_key(backend, monkeypatch):
    monkeypatch.setattr(disk, "_backing", lambda path: {path: "/dev/vda3"})
    assert disk.inspect()["supported"] is False
    with pytest.raises(disk.DiskError, match="separate"):
        disk.prepare("original disk password")


def test_a_shared_container_does_not_hide_an_additional_encrypted_dependency(backend, monkeypatch):
    monkeypatch.setattr(disk, "_backing", lambda path:
                        {"system": "/dev/vda3"} if path == "/var" else
                        {"system": "/dev/vda3", "second": "/dev/vdb3"})
    assert disk.inspect()["encrypted"] is True
    assert disk.inspect()["supported"] is False


def test_wrong_password_creates_no_slot_or_state(backend):
    with pytest.raises(disk.DiskError):
        disk.prepare("wrong")
    assert list(backend["slots"]) == ["0"]
    assert not disk.STATE.exists()


def test_the_generated_key_is_verified_without_removing_the_original(backend):
    key = disk.prepare("original disk password")
    assert backend["slots"] == {"0": "original disk password", "1": key}
    assert len(key.split("-")) == 8
    assert disk.inspect()["recovery_saved"] is False
    metadata = disk.STATE.read_text()
    assert key not in metadata and "original disk password" not in metadata
    assert (disk.STATE.stat().st_mode & 0o777) == 0o600
    assert all(key not in arg and "original disk password" not in arg
               for argv in backend["calls"] for arg in argv)
    assert any("--test-passphrase" in argv and "--key-slot" in argv
               for argv in backend["calls"])


def test_setup_waits_for_the_written_down_key_to_be_checked(backend):
    key = disk.prepare("original disk password")
    with pytest.raises(disk.DiskError, match="Save and verify"):
        disk.require_ready()
    with pytest.raises(disk.DiskError):
        disk.confirm("00000000-" * 7 + "00000000")
    assert disk.inspect()["recovery_saved"] is False
    disk.confirm(key)
    disk.require_ready()
    assert disk.inspect()["recovery_saved"] is True
    with pytest.raises(disk.DiskError, match="already been saved"):
        disk.prepare("original disk password")


def test_a_retry_keeps_the_original_password(backend):
    first = disk.prepare("original disk password")
    second = disk.prepare("original disk password")
    assert first != second
    assert backend["slots"]["0"] == "original disk password"
    assert list(backend["slots"].values()).count(second) == 1


def test_a_pending_recovery_key_cannot_remove_its_only_working_slot(backend):
    key = disk.prepare("original disk password")
    with pytest.raises(disk.DiskError, match="original disk password"):
        disk.prepare(key)
    assert backend["slots"]["1"] == key


def test_a_confirmation_from_a_different_disk_is_not_reused(backend, monkeypatch):
    key = disk.prepare("original disk password")
    disk.confirm(key)
    monkeypatch.setattr(disk, "_backing", lambda path: {"replacement": "/dev/vdb3"})
    assert disk.inspect()["recovery_saved"] is False


def test_a_removed_recovery_slot_is_not_reported_as_ready(backend):
    key = disk.prepare("original disk password")
    disk.confirm(key)
    del backend["slots"]["1"]
    assert disk.inspect()["recovery_saved"] is False


def test_a_reused_confirmed_slot_is_not_reported_as_ready(backend):
    key = disk.prepare("original disk password")
    disk.confirm(key)
    backend["slots"]["1"] = "another owner's unrelated key"
    assert disk.inspect()["recovery_saved"] is False


def test_a_reused_pending_slot_is_never_removed(backend):
    disk.prepare("original disk password")
    backend["slots"]["1"] = "another owner's unrelated key"
    key = disk.prepare("original disk password")
    assert backend["slots"]["1"] == "another owner's unrelated key"
    assert backend["slots"]["2"] == key


def test_a_crash_before_slot_fingerprint_is_saved_cannot_delete_an_unrelated_key(backend):
    disk._write_state({"uuid": "system", "slot": 1, "confirmed": False})
    backend["slots"]["1"] = "another owner's unrelated key"
    disk.prepare("original disk password")
    assert backend["slots"]["1"] == "another owner's unrelated key"


def test_secret_fds_are_closed_even_after_failure():
    with pytest.raises(RuntimeError):
        with disk._key_fd("a password") as fd:
            assert os.read(fd, 4096) == b"a password"
            raise RuntimeError()
    with pytest.raises(OSError):
        os.read(fd, 4096)


def test_tool_output_is_never_reported_in_errors(monkeypatch):
    secret = "not-for-the-journal"
    monkeypatch.setattr(disk.subprocess, "run", lambda *a, **k:
                        subprocess.CompletedProcess(a, 1, secret, secret))
    with pytest.raises(disk.DiskError) as error:
        disk._run(["cryptsetup", "open"])
    assert secret not in str(error.value)


def test_console_recovery_output_cannot_be_piped_into_a_setup_log(monkeypatch, capsys):
    from kosherd import cli

    monkeypatch.setattr(cli, "_isatty", lambda stream: False)
    console = cli._Console()
    with pytest.raises(RuntimeError, match="does not write to a log"):
        console.say_secret("never write this key into tee or journald")
    assert "never write this key" not in capsys.readouterr().out
