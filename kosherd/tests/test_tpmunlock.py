"""Measured boot is a prerequisite, never just the presence of a TPM."""
import hashlib
import json
import os
import struct
import subprocess

import pytest

from kosherd import disksecurity as disk, tpmunlock as tpm


def event(pcr, data):
    digest = hashlib.sha256(data).digest()
    return struct.pack('<IIIH', pcr, 13, 1, 0xb) + digest + struct.pack('<I', len(data)) + data


def log_for(records):
    return bytes(28) + struct.pack('<I', 16) + b'Spec ID Event03\0' + b''.join(
        event(p, data) for p, data in records)


@pytest.fixture
def platform(monkeypatch, tmp_path):
    for name in ('DIRECTORY', 'EFI', 'EVENT_LOG', 'CMDLINE', 'BOOT_ID', 'SETUP_COMPLETE'):
        monkeypatch.setattr(tpm, name, tmp_path / name)
    tpm.EFI.mkdir()
    guid = '8be4df61-93ca-11d2-aa0d-00e098032b8c'
    (tpm.EFI / f'SecureBoot-{guid}').write_bytes(bytes(4) + b'\x01')
    (tpm.EFI / f'SetupMode-{guid}').write_bytes(bytes(5))
    tpm.CMDLINE.write_text('BOOT_IMAGE=/boot/vmlinuz-1 root=UUID=system quiet\n')
    tpm.BOOT_ID.write_text('first-boot')
    records = [(4, b'shim'), (7, b'secure boot'),
               (8, b'kernel_cmdline: /boot/vmlinuz-1 root=UUID=system quiet\0'),
               (9, b'/boot/vmlinuz-1\0'), (9, b'/boot/initramfs-1.img\0')]
    tpm.EVENT_LOG.write_bytes(log_for(records))
    pcrs = {p: bytes(32) for p in tpm.PCRS}
    for p, data in records:
        pcrs[p] = hashlib.sha256(pcrs[p] + hashlib.sha256(data).digest()).digest()
    state = {'info': dict(supported=True, recovery_saved=True, uuid='system', device='/dev/vda3'),
             'metadata': {'keyslots': {'0': {'salt': 'original'}, '1': {'salt': 'recovery'}}, 'tokens': {}},
             'calls': [], 'valid_token': True}
    monkeypatch.setattr(disk, 'inspect', lambda: state['info'])
    monkeypatch.setattr(disk, '_backing', lambda path: {state['info']['uuid']: state['info']['device']})
    def run(argv, **kwargs):
        state['calls'].append(argv)
        output = ''
        if argv[0] == 'tpm2_pcrread':
            output = '\n'.join(f'  {p} : 0x{value.hex()}' for p, value in pcrs.items())
        elif 'luksDump' in argv:
            output = json.dumps(state['metadata'])
        elif argv[0] == 'systemd-cryptenroll':
            fd = int(next(a for a in argv if a.startswith('--unlock-key-file=')).rsplit('/', 1)[1])
            assert os.read(fd, 100) == b'test password'
            slot = str(max(map(int, state['metadata']['keyslots'])) + 1)
            token = str(len(state['metadata']['tokens']))
            state['metadata']['keyslots'][slot] = {'salt': 'new TPM key ' + slot}
            state['metadata']['tokens'][token] = {'type': 'systemd-tpm2', 'keyslots': [slot],
                                                'tpm2-pcrs': list(tpm.PCRS), 'tpm2-pcr-bank': 'sha256'}
        elif '--token-only' in argv:
            if not state['valid_token'] or argv[argv.index('--token-id') + 1] in state.get('bad_tokens', set()):
                raise disk.DiskError('TPM refused')
        elif 'luksKillSlot' in argv:
            slot = argv[-1]
            del state['metadata']['keyslots'][slot]
            state['metadata']['tokens'] = {k: v for k, v in state['metadata']['tokens'].items()
                                          if slot not in v['keyslots']}
        elif '--test-passphrase' in argv:
            fd = int(argv[argv.index('--key-file') + 1].rsplit('/', 1)[1])
            if os.read(fd, 100) != b'test password':
                raise disk.DiskError('wrong password')
        return subprocess.CompletedProcess(argv, 0, output, '')
    monkeypatch.setattr(disk, '_run', run)
    return state


def test_complete_measured_boot_is_accepted(platform):
    tpm.boot_policy()


@pytest.mark.parametrize('change', ['secureboot', 'setupmode', 'cmdline', 'initrd', 'digest', 'legacy', 'truncated'])
def test_incomplete_or_changed_boot_is_refused(platform, change):
    if change in ('secureboot', 'setupmode'):
        name = 'SecureBoot' if change == 'secureboot' else 'SetupMode'
        value = 0 if change == 'secureboot' else 1
        next(tpm.EFI.glob(name + '-*')).write_bytes(bytes(4) + bytes([value]))
    elif change == 'cmdline':
        tpm.CMDLINE.write_text(tpm.CMDLINE.read_text().strip() + ' init=/bin/sh')
    else:
        blob = tpm.EVENT_LOG.read_bytes()
        if change == 'initrd':
            blob = blob.replace(b'initramfs', b'otherfile')
        elif change == 'digest':
            blob = blob[:62] + bytes([blob[62] ^ 1]) + blob[63:]
        elif change == 'legacy':
            blob = bytes(len(blob))
        else:
            blob = blob[:-3]
        tpm.EVENT_LOG.write_bytes(blob)
    with pytest.raises(disk.DiskError, match='measured'):
        tpm.enroll('test password')
    assert not any(c[0] == 'systemd-cryptenroll' for c in platform['calls'])


def test_enroll_requires_recovery_and_correct_password(platform):
    platform['info']['recovery_saved'] = False
    with pytest.raises(disk.DiskError, match='recovery'):
        tpm.enroll('test password')
    platform['info']['recovery_saved'] = True
    with pytest.raises(disk.DiskError, match='wrong'):
        tpm.enroll('wrong password')
    assert not any(c[0] == 'systemd-cryptenroll' for c in platform['calls'])


def test_real_policy_is_explicit_and_password_never_enters_arguments(platform):
    tpm.enroll('test password')
    call = next(c for c in platform['calls'] if c[0] == 'systemd-cryptenroll')
    assert '--tpm2-pcrs=4:sha256+7:sha256+8:sha256+9:sha256' in call
    assert '--tpm2-public-key=' in call and '--tpm2-pcrlock=' in call
    assert not any('test password' in arg or '--wipe-slot' in arg for c in platform['calls'] for arg in c)
    assert platform['metadata']['keyslots']['0'] == {'salt': 'original'}
    assert platform['metadata']['keyslots']['1'] == {'salt': 'recovery'}
    assert tpm.status()['enrolled']
    assert 'password' not in (tpm.DIRECTORY / 'enrollment.json').read_text()


def test_failed_token_does_not_claim_enrollment(platform):
    platform['valid_token'] = False
    with pytest.raises(disk.DiskError):
        tpm.enroll('test password')
    assert not (tpm.DIRECTORY / 'enrollment.json').exists()
    assert set(platform['metadata']['keyslots']) == {'0', '1'}
    assert not platform['metadata']['tokens']


def test_pending_enrollment_waits_for_finished_setup_and_next_boot(platform):
    tpm.schedule('test password', True)
    pending = tpm.DIRECTORY / 'pending.json'
    assert pending.stat().st_mode & 0o777 == 0o600
    assert tpm.DIRECTORY.stat().st_mode & 0o777 == 0o700
    tpm.finish_pending()
    assert not tpm.status()['enrolled']
    tpm.SETUP_COMPLETE.touch()
    tpm.finish_pending()
    assert not tpm.status()['enrolled']
    tpm.BOOT_ID.write_text('next-boot')
    tpm.finish_pending()
    assert tpm.status()['enrolled'] and not pending.exists()


def test_cancel_removes_pending_secret_and_never_changes_luks(platform):
    tpm.schedule('test password', True)
    platform['calls'].clear()
    tpm.schedule('', False)
    assert not (tpm.DIRECTORY / 'pending.json').exists()
    assert not platform['calls']


def test_pending_enrollment_cannot_follow_a_replaced_disk(platform):
    tpm.schedule('test password', True)
    tpm.BOOT_ID.write_text('next-boot')
    tpm.SETUP_COMPLETE.touch()
    platform['info']['uuid'] = 'different'
    with pytest.raises(disk.DiskError, match='changed'):
        tpm.finish_pending()


def test_setup_closes_and_reenrollment_requires_admin_and_guardian():
    from kosherd import access
    params = dict(setup_complete=True, session_unlocked=False,
                  guardian_enabled=True, guardian_proven=False)
    assert not access.evaluate('SetupDiskTpm', **params).allowed
    req = access.evaluate('EnrollDiskTpm', **params)
    assert req.polkit_action == access.ACTION_MANAGE_USERS and req.needs_guardian
    assert {'SetupDiskTpm', 'EnrollDiskTpm'} <= access.NO_ARGS_LOGGED


def test_repeated_enrollment_does_not_consume_another_slot(platform):
    tpm.enroll('test password')
    platform['calls'].clear()
    tpm.enroll('test password')
    assert not any(c[0] == 'systemd-cryptenroll' for c in platform['calls'])


def test_no_encryption_never_offers_tpm(platform):
    platform['info']['supported'] = False
    assert not tpm.status()['available']
    with pytest.raises(disk.DiskError):
        tpm.schedule('test password', True)
    assert not (tpm.DIRECTORY / 'pending.json').exists()


def test_pending_secret_must_be_on_the_encrypted_system_disk(platform, monkeypatch):
    monkeypatch.setattr(disk, '_backing', lambda path: {})
    with pytest.raises(disk.DiskError, match='state directory'):
        tpm.schedule('test password', True)
    assert not (tpm.DIRECTORY / 'pending.json').exists()


def test_reenrollment_removes_only_our_verified_old_tpm_slot(platform):
    tpm.enroll('test password')
    platform['bad_tokens'] = {'0'}
    tpm.enroll('test password')
    assert set(platform['metadata']['keyslots']) == {'0', '1', '3'}
    assert set(platform['metadata']['tokens']) == {'1'}
    assert tpm.status()['enrolled']


def test_changed_old_slot_is_never_removed(platform):
    tpm.enroll('test password')
    platform['metadata']['keyslots']['2'] = {'salt': 'somebody else'}
    tpm.enroll('test password')
    assert set(platform['metadata']['keyslots']) == {'0', '1', '2', '3'}
    assert not any('luksKillSlot' in c for c in platform['calls'])


def test_cli_never_reads_disk_password_from_a_logged_pipe(monkeypatch, capsys):
    from types import SimpleNamespace
    from kosherd import cli
    client = SimpleNamespace(disk_unlock_status=lambda: {'available': True, 'message': 'Available'})
    monkeypatch.setattr(cli, '_client', lambda: client)
    monkeypatch.setattr(cli._Console, 'secret_terminal', staticmethod(lambda: False))
    assert cli.cmd_disk_tpm(SimpleNamespace(action='enroll')) == 1
    assert 'direct terminal' in capsys.readouterr().err


def test_an_unexpected_weak_policy_is_removed_before_returning(platform, monkeypatch):
    original = disk._run
    def run(argv, **kwargs):
        result = original(argv, **kwargs)
        if argv[0] == 'systemd-cryptenroll':
            platform['metadata']['tokens']['0']['tpm2-pcrs'] = [7]
        return result
    monkeypatch.setattr(disk, '_run', run)
    with pytest.raises(disk.DiskError, match='policy'):
        tpm.enroll('test password')
    assert set(platform['metadata']['keyslots']) == {'0', '1'}
    assert not platform['metadata']['tokens']
