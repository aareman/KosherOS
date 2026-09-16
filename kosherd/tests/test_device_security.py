"""The device-security checks an operating system can answer for.

GNOME's Device Security panel reports fwupd's Host Security ID. Most of
that score belongs to the firmware and the CPU, but three checks are the
OS's, and an image that leaves them unset fails them for no reason.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).parents[2]
CONTAINERFILE = ROOT / "os-image/Containerfile"
KARGS = "/usr/lib/bootc/kargs.d/20-kosheros-security.toml"


def kargs_written() -> dict[str, list[str]]:
    """{file written: the kargs written into it} from the Containerfile."""
    text = CONTAINERFILE.read_text()
    found = {}
    for match in re.finditer(r"printf 'kargs = \[([^\]]*)\].*?> (\S+)", text, re.S):
        found[match.group(2)] = re.findall(r'"([^"]+)"', match.group(1))
    return found


def test_the_three_checks_an_os_can_answer_are_answered():
    kargs = kargs_written().get(KARGS)
    assert kargs, f"{KARGS} is not written by the image"
    assert "lockdown=integrity" in kargs, "root must not be able to rewrite the kernel"
    assert "intel_iommu=on" in kargs, "devices behind the IOMMU"
    assert "mem_sleep_default=s2idle" in kargs, "s2idle, not deep S3"


def test_the_quiet_boot_arguments_are_a_separate_file():
    # Two concerns, two files: a change to how quiet the console is must
    # not be able to drop the security arguments by accident.
    files = kargs_written()
    assert "/usr/lib/bootc/kargs.d/10-kosheros.toml" in files
    assert "quiet" in files["/usr/lib/bootc/kargs.d/10-kosheros.toml"]
    assert "quiet" not in files[KARGS]


def test_nothing_here_promises_a_green_panel():
    # The firmware and the CPU decide most of that score, and a family that
    # opens the panel should be told so rather than left to conclude the
    # filter is broken.
    doc = (ROOT / "docs/architecture.md").read_text()
    assert "Device Security" in doc
    for karg in ("lockdown=integrity", "intel_iommu=on", "mem_sleep_default=s2idle"):
        assert karg in doc, karg
    for theirs in ("BootGuard", "Secure Boot", "TPM", "zram"):
        assert theirs in doc, theirs
    assert "not a verdict on the filter" in doc
