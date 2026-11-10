"""Inspect the system disk and give its owner a separately verified recovery key.

Only LUKS2 containers backing the actual system, /etc and /var count. A plugged-in
encrypted drive is not evidence that the policy on the system disk is safe.
Passwords travel to cryptsetup through anonymous file descriptors, never
argv, the environment, a log or a temporary file. The installer passphrase
is retained: generating a recovery key must never remove a working unlock.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import secrets
import subprocess
import threading
from pathlib import Path

STATE = Path("/var/lib/kosher/disk-recovery.json")
_LOCK = threading.RLock()


class DiskError(Exception):
    """The disk could not be inspected or its recovery key could not be set."""


def _run(argv: list[str], **kwargs) -> subprocess.CompletedProcess:
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=120, **kwargs)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DiskError("The disk encryption tool could not finish. Try again.") from exc
    if result.returncode:
        # Do not return a tool's output: disk passwords must not enter a
        # D-Bus error, activity entry or journal, even on a failed command.
        raise DiskError("The disk password was not accepted, or the disk could not be updated.")
    return result


def _json(argv: list[str]) -> dict:
    try:
        return json.loads(_run(argv).stdout)
    except (ValueError, TypeError) as exc:
        raise DiskError("The disk encryption information could not be read.") from exc


def luks_ancestors(tree: dict) -> dict[str, str]:
    """LUKS ancestors only if EVERY storage branch crosses encryption.

    A striped/mirrored filesystem with one encrypted member and one plain
    member is not protected. lsblk must include the inverse tree, not a
    flat collection from which the branch relationships are lost.
    """
    result = {}

    def walk(node, encrypted=False):
        if node.get("fstype") == "crypto_LUKS" and node.get("uuid"):
            path = node.get("path", "")
            if path.startswith("/dev/"):
                result[node["uuid"]] = path
                encrypted = True
        children = node.get("children") or []
        return all(walk(child, encrypted) for child in children) if children else encrypted

    roots = tree.get("blockdevices") or []
    return result if roots and all(walk(node) for node in roots) else {}


def _backing(path: str) -> dict[str, str]:
    source = _run(["findmnt", "--noheadings", "--output", "MAJ:MIN", "--target", path]).stdout.strip()
    if not re.fullmatch(r"\d+:\d+", source):
        raise DiskError("The system disk could not be identified.")
    return luks_ancestors(_json([
        "lsblk", "--json", "--inverse", "--tree", "--output", "PATH,TYPE,FSTYPE,UUID",
        f"/dev/block/{source}",
    ]))


def _read_state() -> dict:
    try:
        doc = json.loads(STATE.read_text())
        if isinstance(doc, dict):
            return doc
    except (OSError, ValueError):
        pass
    return {}


def _write_state(doc: dict) -> None:
    STATE.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = STATE.with_suffix(".new")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump(doc, stream)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, STATE)
        directory = os.open(STATE.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def _slot_fingerprint(metadata: dict, slot: str) -> str | None:
    entry = metadata.get("keyslots", {}).get(slot)
    if entry is None:
        return None
    encoded = json.dumps(entry, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def inspect() -> dict:
    """Public status, containing no password, recovery key or LUKS metadata."""
    system = "/sysroot" if Path("/sysroot").is_mount() else "/"
    root = _backing(system)
    var = _backing("/var")
    etc = _backing("/etc")
    volumes = root.keys() | var.keys() | etc.keys()
    if not root or not var or not etc:
        return {"encrypted": False, "supported": False, "recovery_saved": False,
                "message": "This system disk is not encrypted. Another operating system "
                           "can read or change the filter settings on the disk. Reinstall "
                           "with disk encryption to protect them."}
    if len(volumes) != 1:
        return {"encrypted": True, "supported": False, "recovery_saved": False,
                "message": "The system uses separate encrypted disks. Keep the unlock "
                           "and recovery information for each disk from the installer."}
    uuid = next(iter(volumes))
    device = root[uuid]
    metadata = _json(["cryptsetup", "luksDump", "--dump-json-metadata", device])
    if not isinstance(metadata.get("keyslots"), dict):
        raise DiskError("The system disk needs LUKS2 recovery support.")
    state = _read_state()
    slot = str(state.get("slot", ""))
    saved = state.get("uuid") == uuid and state.get("confirmed") is True \
        and state.get("fingerprint") is not None \
        and state["fingerprint"] == _slot_fingerprint(metadata, slot)
    return {"encrypted": True, "supported": True, "uuid": uuid,
            "device": device, "recovery_saved": saved,
            "message": "The system disk is encrypted. Keep its disk password and "
                       "recovery key somewhere outside this computer."}


@contextlib.contextmanager
def _key_fd(value: str):
    if not value or "\0" in value or len(value) > 4096:
        raise DiskError("Enter the disk password.")
    fd = os.memfd_create("kosher-disk-key", os.MFD_CLOEXEC)
    try:
        os.write(fd, value.encode("utf-8"))
        os.lseek(fd, 0, os.SEEK_SET)
        yield fd
    finally:
        os.close(fd)


def _verify(device: str, key: str, slot: int | None = None) -> None:
    with _key_fd(key) as fd:
        args = ["cryptsetup", "open", "--test-passphrase", "--type", "luks2",
                "--key-file", f"/proc/self/fd/{fd}"]
        if slot is not None:
            args += ["--key-slot", str(slot)]
        _run([*args, device], pass_fds=(fd,))


def prepare(passphrase: str) -> str:
    with _LOCK:
        return _prepare(passphrase)


def _prepare(passphrase: str) -> str:
    """Add a new recovery key after proving an existing unlock still works.

    A retry replaces only a previously UNCONFIRMED slot this function
    recorded. Confirmed recovery keys and all installer/TPM slots remain.
    The key is returned once; only its slot/UUID are persisted, not its text.
    """
    disk = inspect()
    if not disk["supported"]:
        raise DiskError(disk["message"])
    if disk["recovery_saved"]:
        raise DiskError("A recovery key has already been saved for this disk.")
    device, uuid = disk["device"], disk["uuid"]
    _verify(device, passphrase)
    metadata = _json(["cryptsetup", "luksDump", "--dump-json-metadata", device])
    previous = _read_state()
    old_slot = str(previous.get("slot", ""))
    if previous.get("uuid") == uuid and previous.get("confirmed") is False \
            and old_slot.isdigit() and old_slot in metadata["keyslots"] \
            and previous.get("fingerprint") is not None \
            and previous["fingerprint"] == _slot_fingerprint(metadata, old_slot):
        # Prove the supplied password unlocks SOME OTHER slot before any
        # removal. It could itself be the pending recovery key.
        other = [int(n) for n in metadata["keyslots"] if n != old_slot]
        for slot in other:
            try:
                _verify(device, passphrase, slot)
                break
            except DiskError:
                continue
        else:
            raise DiskError("Enter the original disk password to replace an unsaved recovery key.")
        with _key_fd(passphrase) as fd:
            _run(["cryptsetup", "luksKillSlot", "--batch-mode", "--key-file",
                  f"/proc/self/fd/{fd}", device, old_slot], pass_fds=(fd,))
        del metadata["keyslots"][old_slot]
    slot = next((n for n in range(32) if str(n) not in metadata["keyslots"]), None)
    if slot is None:
        raise DiskError("This disk has no free recovery-key slot.")
    # 256 random bits, grouped for writing down and checking. No ambiguous
    # case or punctuation; the full key is verified before setup continues.
    raw = secrets.token_hex(32)
    key = "-".join(raw[n:n + 8] for n in range(0, len(raw), 8))
    # Record before adding, then persist the slot's salted metadata
    # fingerprint. A crash between those writes may orphan a slot, but a
    # retry must never erase a slot it cannot prove is still our own.
    _write_state({"uuid": uuid, "slot": slot, "confirmed": False})
    with _key_fd(passphrase) as old, _key_fd(key) as new:
        _run(["cryptsetup", "luksAddKey", "--batch-mode", "--key-slot", str(slot),
              "--key-file", f"/proc/self/fd/{old}", device, f"/proc/self/fd/{new}"],
             pass_fds=(old, new))
    metadata = _json(["cryptsetup", "luksDump", "--dump-json-metadata", device])
    fingerprint = _slot_fingerprint(metadata, str(slot))
    if fingerprint is None:
        raise DiskError("The new disk recovery slot could not be verified.")
    _write_state({"uuid": uuid, "slot": slot, "confirmed": False,
                  "fingerprint": fingerprint})
    _verify(device, key, slot)
    return key


def confirm(key: str) -> None:
    """Verify the written-down key against its precise slot, then forget it."""
    with _LOCK:
        _confirm(key)


def _confirm(key: str) -> None:
    disk, state = inspect(), _read_state()
    if not disk["supported"] or state.get("uuid") != disk.get("uuid") \
            or not isinstance(state.get("slot"), int):
        raise DiskError("Create a recovery key first.")
    metadata = _json(["cryptsetup", "luksDump", "--dump-json-metadata", disk["device"]])
    if state.get("fingerprint") is None or state["fingerprint"] != \
            _slot_fingerprint(metadata, str(state["slot"])):
        raise DiskError("The recovery slot changed. Create and save a new recovery key.")
    key = key.strip().lower()
    if not re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{8}){7}", key):
        raise DiskError("Enter all eight groups of the recovery key.")
    _verify(disk["device"], key, state["slot"])
    _write_state({**state, "confirmed": True})


def require_ready() -> None:
    with _LOCK:
        status = inspect()
        if status["supported"] and not status["recovery_saved"]:
            raise DiskError("Save and verify the disk recovery key before finishing setup.")


def encrypted_root_layout(tree: dict) -> tuple[str, str, str] | None:
    """The supported plain ext4-in-LUKS-on-partition layout, or no change.

    LVM, RAID, nested encryption and shared/multiple parents require their
    own volume manager. Never infer a disk to resize from those topologies.
    """
    roots = tree.get("blockdevices") or []
    if len(roots) != 1:
        return None
    mapping = roots[0]
    parents = mapping.get("children") or []
    if mapping.get("type") != "crypt" or mapping.get("fstype") != "ext4" or len(parents) != 1:
        return None
    partition = parents[0]
    disks = partition.get("children") or []
    if partition.get("type") != "part" or len(disks) != 1 or disks[0].get("type") != "disk":
        return None
    paths = tuple(n.get("path", "") for n in (mapping, partition, disks[0]))
    return paths if all(p.startswith("/dev/") for p in paths) else None


def grow_root(target: str | None = None, *, passphrase: str) -> bool:
    """Extend only the existing encrypted-root partition and its mapping.

    growpart never moves another partition or consumes it. Run all three
    steps on every attempt: an interrupted previous growth may have grown
    the partition but not its mapping or filesystem yet.
    """
    if Path("/etc/kosher/keep-disk-layout").exists():
        return False
    if target is None:
        target = "/sysroot" if Path("/sysroot").is_mount() else "/"
    source = _run(["findmnt", "--noheadings", "--output", "MAJ:MIN", "--target", target]).stdout.strip()
    if not re.fullmatch(r"\d+:\d+", source):
        raise DiskError("The system disk could not be identified for growth.")
    layout = encrypted_root_layout(_json([
        "lsblk", "--json", "--inverse", "--tree", "--output", "PATH,TYPE,FSTYPE",
        f"/dev/block/{source}",
    ]))
    if layout is None:
        return False
    mapping, partition, device = layout
    # cryptsetup's mapping key may be in another service's keyring. Never
    # store it for a boot unit: grow while setup has the person's password.
    _verify(partition, passphrase)
    number = (Path("/sys/class/block") / Path(partition).name / "partition").read_text().strip()
    if not number.isdigit():
        raise DiskError("The encrypted root partition could not be identified.")
    result = subprocess.run(["growpart", device, number], capture_output=True, text=True, timeout=120)
    if result.returncode and not (result.returncode == 1 and "NOCHANGE:" in result.stdout):
        raise DiskError("The encrypted root partition could not be grown.")
    with _key_fd(passphrase) as fd:
        _run(["cryptsetup", "resize", "--batch-mode", "--key-file",
              f"/proc/self/fd/{fd}", mapping], pass_fds=(fd,))
    _run(["resize2fs", mapping])
    return True
