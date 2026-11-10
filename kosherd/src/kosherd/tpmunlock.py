"""Optional TPM unlock, sealed to the complete measured GRUB boot.

Never enroll against PCR 7 alone: a different signed kernel or a replaced
initrd must not get the key. Firmware/boot updates deliberately require the
saved disk password and explicit enrollment again. No password slot is removed.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import struct
from pathlib import Path

from . import disksecurity as disk

DIRECTORY = Path('/var/lib/kosher/tpm')
EFI = Path('/sys/firmware/efi/efivars')
EVENT_LOG = Path('/sys/kernel/security/tpm0/binary_bios_measurements')
CMDLINE = Path('/proc/cmdline')
BOOT_ID = Path('/proc/sys/kernel/random/boot_id')
SETUP_COMPLETE = Path('/var/lib/kosher/setup-complete')
PCRS = (4, 7, 8, 9)
PCR_POLICY = '+'.join(f'{p}:sha256' for p in PCRS)


def events(blob: bytes) -> list[tuple[int, bytes, bytes]]:
    """Read the SHA256 events from a bounded TPM2 crypto-agile event log."""
    if len(blob) > 16 * 1024 * 1024 or len(blob) < 32:
        raise ValueError('Invalid event log')
    # The first record is the legacy-format Spec ID event.
    size = struct.unpack_from('<I', blob, 28)[0]
    if blob[32:48] != b'Spec ID Event03\0':
        raise ValueError('Not a TPM2 event log')
    pos = 32 + size
    records = []
    sizes = {0x0004: 20, 0x000b: 32, 0x000c: 48, 0x000d: 64}
    while pos < len(blob):
        pcr, kind, count = struct.unpack_from('<III', blob, pos)
        pos += 12
        if count > 8:
            raise ValueError('Invalid digest count')
        sha256 = None
        for _ in range(count):
            alg = struct.unpack_from('<H', blob, pos)[0]
            pos += 2
            length = sizes[alg]
            if alg == 0x000b:
                sha256 = blob[pos:pos + length]
            pos += length
        length = struct.unpack_from('<I', blob, pos)[0]
        pos += 4
        data = blob[pos:pos + length]
        pos += length
        if pos > len(blob):
            raise ValueError('Truncated event log')
        if sha256 is not None and kind != 3:  # EV_NO_ACTION is not extended
            records.append((pcr, sha256, data))
    return records


def boot_policy() -> None:
    """Refuse unsupported/unmeasured boots before touching any LUKS slot."""
    try:
        guid = '8be4df61-93ca-11d2-aa0d-00e098032b8c'
        if (EFI / f'SecureBoot-{guid}').read_bytes()[4:] != b'\x01' or \
                (EFI / f'SetupMode-{guid}').read_bytes()[4:] != b'\x00':
            raise ValueError('Secure Boot disabled')
        records = events(EVENT_LOG.read_bytes())
        cmdline = CMDLINE.read_text().strip()
        if not cmdline.startswith('BOOT_IMAGE='):
            raise ValueError('Unsupported bootloader')
        expected = ('kernel_cmdline: ' + cmdline.removeprefix('BOOT_IMAGE=')).encode()
        if not any(p == 8 and data.rstrip(b'\0') == expected for p, _, data in records):
            raise ValueError('Unmeasured command line')
        files = [data for p, _, data in records if p == 9]
        if not any(b'/vmlinuz-' in data for data in files) or \
                not any(b'/initramfs-' in data for data in files):
            raise ValueError('Unmeasured kernel/initrd')
        replay = {p: bytes(32) for p in PCRS}
        for p, digest, _ in records:
            if p in replay:
                replay[p] = hashlib.sha256(replay[p] + digest).digest()
        output = disk._run(['tpm2_pcrread', 'sha256:4,7,8,9']).stdout
        current = {int(p): bytes.fromhex(value) for p, value in
                   re.findall(r'^\s*(\d+)\s*:\s*0x([0-9a-fA-F]{64})\s*$', output, re.M)}
        if any(replay[p] == bytes(32) or current.get(p) != replay[p] for p in PCRS):
            raise ValueError('PCRs do not match the boot log')
    except (OSError, ValueError, KeyError, struct.error, disk.DiskError):
        raise disk.DiskError('Automatic unlock needs TPM 2.0, Secure Boot, and a measured '
                             'KosherOS boot. Keep using the disk password on this computer.') from None


def _read(name: str) -> dict:
    try:
        result = json.loads((DIRECTORY / name).read_text())
        return result if isinstance(result, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(name: str, value: dict) -> None:
    DIRECTORY.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = DIRECTORY / (name + '.new')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, DIRECTORY / name)
        fd = os.open(DIRECTORY, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        temporary.unlink(missing_ok=True)


def status(info: dict | None = None) -> dict:
    info = disk.inspect() if info is None else info
    if not info.get('supported'):
        return {'available': False, 'pending': False, 'enrolled': False,
                'message': 'Automatic unlock needs an encrypted system disk.'}
    try:
        boot_policy()
    except disk.DiskError as error:
        return {'available': False, 'pending': False, 'enrolled': False, 'message': str(error)}
    pending = _read('pending.json').get('uuid') == info['uuid']
    metadata = disk._json(['cryptsetup', 'luksDump', '--dump-json-metadata', info['device']])
    saved = _read('enrollment.json')
    token = metadata.get('tokens', {}).get(str(saved.get('token')), {})
    enrolled = saved.get('uuid') == info['uuid'] and token.get('type') == 'systemd-tpm2' \
        and token.get('tpm2-pcrs') == list(PCRS) and token.get('tpm2-pcr-bank') == 'sha256' \
        and saved.get('fingerprint') == disk._slot_fingerprint(metadata, str(saved.get('slot')))
    return {'available': True, 'pending': pending, 'enrolled': bool(enrolled),
            'message': ('Automatic unlock is scheduled. Enter the disk password once on the '
                        'next restart; later normal starts unlock automatically.' if pending else
                        'Automatic unlock is enrolled. A changed boot or firmware may still '
                        'ask for your saved disk password.' if enrolled else
                        'You can use this computer\u2019s TPM to unlock the encrypted disk. '
                        'Boot or firmware updates may require your saved disk password.')}


def _ready(password: str) -> dict:
    info = disk.inspect()
    if not info.get('supported') or not info.get('recovery_saved'):
        raise disk.DiskError('Save and verify the encrypted disk recovery key first.')
    boot_policy()
    DIRECTORY.mkdir(mode=0o700, parents=True, exist_ok=True)
    if disk._backing(str(DIRECTORY)) != {info['uuid']: info['device']}:
        raise disk.DiskError('Automatic unlock needs its state directory on the encrypted system disk.')
    disk._verify(info['device'], password)
    return info


def schedule(password: str, enabled: bool) -> None:
    """First-boot setup changes GRUB: enroll only after the next manual unlock.

    The credential is kept root-only INSIDE the encrypted system disk until
    that boot. It is never put in /boot, the initrd, argv, environment or logs.
    """
    with disk._LOCK:
        if not enabled:
            (DIRECTORY / 'pending.json').unlink(missing_ok=True)
            return
        info = _ready(password)
        _write('pending.json', {'uuid': info['uuid'], 'password': password,
                               'boot_id': BOOT_ID.read_text().strip()})


def enroll(password: str) -> None:
    with disk._LOCK:
        info = _ready(password)
        device = info['device']
        before = disk._json(['cryptsetup', 'luksDump', '--dump-json-metadata', device])
        previous = _read('enrollment.json')
        old_token = before.get('tokens', {}).get(str(previous.get('token')), {})
        owned = previous.get('uuid') == info['uuid'] and old_token.get('type') == 'systemd-tpm2' \
            and old_token.get('tpm2-pcrs') == list(PCRS) and old_token.get('tpm2-pcr-bank') == 'sha256' \
            and old_token.get('keyslots') == [str(previous.get('slot'))] \
            and previous.get('fingerprint') is not None \
            and previous['fingerprint'] == disk._slot_fingerprint(before, str(previous.get('slot')))
        if owned:
            try:
                disk._run(['cryptsetup', 'open', '--test-passphrase', '--token-only',
                           '--token-id', str(previous['token']), device])
                return  # Already unlocks this boot: no extra slot on a retry.
            except disk.DiskError:
                pass
        # Add and prove the replacement before removing our old slot. Never
        # use --wipe-slot=tpm2: that would remove enrollments owned by others.
        with disk._key_fd(password) as fd:
            disk._run(['systemd-cryptenroll', '--tpm2-device=auto',
                       f'--tpm2-pcrs={PCR_POLICY}', '--tpm2-with-pin=no',
                       '--tpm2-public-key=', '--tpm2-pcrlock=',
                       f'--unlock-key-file=/proc/self/fd/{fd}', device], pass_fds=(fd,))
        after = disk._json(['cryptsetup', 'luksDump', '--dump-json-metadata', device])
        added = set(after.get('tokens', {})) - set(before.get('tokens', {}))
        try:
            if len(added) != 1:
                raise disk.DiskError('The TPM enrollment could not be verified. Your disk password still works.')
            token_id = added.pop()
            token = after['tokens'][token_id]
            slots = token.get('keyslots', [])
            if token.get('type') != 'systemd-tpm2' or token.get('tpm2-pcrs') != list(PCRS) \
                    or token.get('tpm2-pcr-bank') != 'sha256' or len(slots) != 1:
                raise disk.DiskError('The TPM policy could not be verified. Your disk password still works.')
            # Prove the TPM token works now, without allowing a password fallback.
            disk._run(['cryptsetup', 'open', '--test-passphrase', '--token-only',
                       '--token-id', token_id, device])
        except disk.DiskError:
            # Fail closed: do not leave an unexpectedly weak or unverified
            # token behind. Remove ONLY slots created by this attempt whose
            # salted metadata still matches, never any pre-existing unlock.
            fresh = disk._json(['cryptsetup', 'luksDump', '--dump-json-metadata', device])
            for new_slot in set(after.get('keyslots', {})) - set(before.get('keyslots', {})):
                if disk._slot_fingerprint(fresh, new_slot) == disk._slot_fingerprint(after, new_slot):
                    with disk._key_fd(password) as fd:
                        disk._run(['cryptsetup', 'luksKillSlot', '--batch-mode', '--key-file',
                                   f'/proc/self/fd/{fd}', device, new_slot], pass_fds=(fd,))
            raise
        _write('enrollment.json', {'uuid': info['uuid'], 'token': token_id, 'slot': slots[0],
                                  'fingerprint': disk._slot_fingerprint(after, slots[0])})
        if owned:
            # Only a slot with our salted fingerprint and no other token owner.
            old_slot = str(previous['slot'])
            references = [k for k, v in after.get('tokens', {}).items()
                          if old_slot in v.get('keyslots', [])]
            if references == [str(previous['token'])] and \
                    disk._slot_fingerprint(after, old_slot) == previous['fingerprint']:
                with disk._key_fd(password) as fd:
                    disk._run(['cryptsetup', 'luksKillSlot', '--batch-mode', '--key-file',
                               f'/proc/self/fd/{fd}', device, old_slot], pass_fds=(fd,))


def finish_pending() -> None:
    with disk._LOCK:
        pending = _read('pending.json')
        if not pending or not SETUP_COMPLETE.exists() or \
                pending.get('boot_id') == BOOT_ID.read_text().strip():
            return
        if disk.inspect().get('uuid') != pending.get('uuid'):
            raise disk.DiskError('The system disk changed; automatic unlock was not enrolled.')
        enroll(pending['password'])
        (DIRECTORY / 'pending.json').unlink()


def main() -> None:
    try:
        finish_pending()
    except disk.DiskError as error:
        # Deliberately only our secret-free error, never subprocess output.
        raise SystemExit(str(error)) from None


if __name__ == '__main__':
    main()
