#!/usr/bin/env python3
"""Exercise real LUKS/TPM operations and reboot with Secure Boot, without sudo.

Pass a set-up KosherOS qcow2 and its SSH key. Only a new overlay is written;
this tests a disposable LUKS container, not encryption of the supplied disk.
Full encrypted-root install acceptance is separate (docs/install.md).
"""
from __future__ import annotations

import argparse
import importlib.util
import sys
import time
from pathlib import Path


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--disk', type=Path, required=True)
    p.add_argument('--key', type=Path, required=True)
    p.add_argument('--out', type=Path, required=True)
    args = p.parse_args()
    root = Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location('pr_harness', root / 'scripts/pr-test.py')
    h = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = h
    spec.loader.exec_module(h)
    out = args.out.resolve()
    if out.exists():
        p.error('--out must be a new directory, so TPM state is never reused accidentally')
    out.mkdir(parents=True)
    disk = h.child_overlay(args.disk.resolve(), out / 'machine.qcow2')
    vm = h.Machine(disk, out, args.key.resolve(), False, tpm=True)
    evidence = []
    def check(command, label, *, input=None):
        code, text = vm.sh(command, timeout=180, input=input)
        if code:
            raise RuntimeError(f'{label}: {text}')
        evidence.append(label + ': PASS')
        (out / 'results.txt').write_text('\n'.join(evidence) + '\n')
        print(evidence[-1], flush=True)
        return text
    try:
        if not vm.wait_ssh():
            raise RuntimeError('Secure Boot VM did not start')
        source = (root / 'kosherd/src/kosherd/tpmunlock.py').read_text()
        check('cat > /tmp/tpm-under-test.py', 'Upload current TPM implementation', input=source)
        setup = '''
import importlib.util,json,subprocess
from pathlib import Path
from kosherd import disksecurity as disk
spec=importlib.util.spec_from_file_location('kosherd.tpmunlock','/tmp/tpm-under-test.py')
tpm=importlib.util.module_from_spec(spec);spec.loader.exec_module(tpm)
tpm.DIRECTORY=Path('/var/tmp/kosher-tpm-test');tpm.DIRECTORY.mkdir(exist_ok=True)
image=tpm.DIRECTORY/'luks.img'
with image.open('wb') as f:f.truncate(64*1024*1024)
subprocess.run(['cryptsetup','luksFormat','--type=luks2','--batch-mode','--pbkdf=pbkdf2','--key-file=-',str(image)],input=b'tpm-test-only-password',check=True)
uuid=subprocess.check_output(['cryptsetup','luksUUID',str(image)],text=True).strip()
# Test container only: never point this fixture at the actual system disk.
disk._backing=lambda path:{uuid:str(image)}
disk.STATE=tpm.DIRECTORY/'recovery.json'
key=disk.prepare('tpm-test-only-password');disk.confirm(key)
(tpm.DIRECTORY/'test-recovery').write_text(key)
(tpm.DIRECTORY/'test-recovery').chmod(0o600)
tpm.enroll('tpm-test-only-password')
tpm.enroll('tpm-test-only-password')
assert tpm.status()['enrolled']
assert len(disk._json(['cryptsetup','luksDump','--dump-json-metadata',str(image)])['keyslots'])==3
'''
        check('python3 -', 'Recovery, measured enrollment, token unlock, idempotent retry', input=setup)
        boot_id = check('cat /proc/sys/kernel/random/boot_id', 'Record initial boot').strip()
        vm.sh('systemctl reboot')
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            code, text = vm.sh('cat /proc/sys/kernel/random/boot_id', timeout=10)
            if code == 0 and text.strip() != boot_id:
                break
            time.sleep(3)
        else:
            raise RuntimeError('VM did not reboot')
        image = '/var/tmp/kosher-tpm-test/luks.img'
        check(f'cryptsetup open --test-passphrase --token-only {image}', 'TPM unlock after restart')
        # Change the measured command-line PCR as a hostile boot would. The
        # test must prove rejection, not just the existence of a TPM token.
        check('tpm2_pcrextend 8:sha256=' + '11' * 32, 'Change boot measurement')
        code, _ = vm.sh(f'cryptsetup open --test-passphrase --token-only {image}')
        if code == 0:
            raise RuntimeError('TPM unlocked after a changed boot measurement')
        evidence.append('Changed boot measurement refused: PASS')
        check(f'cryptsetup open --test-passphrase --key-file=- {image}',
              'Original password survives TPM refusal', input='tpm-test-only-password')
        check(f'cryptsetup open --test-passphrase --key-file=/var/tmp/kosher-tpm-test/test-recovery {image}',
              'Verified recovery key survives TPM refusal')
    finally:
        vm.stop()


if __name__ == '__main__':
    main()
