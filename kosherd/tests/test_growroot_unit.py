"""The root partition takes the whole disk, on real hardware too.

A laptop installed from the ISO had 178 GB of its disk unallocated and
ran out of room for an update: the installer's automatic partitioning
stops the root at 70 GiB, and bootc's unit that grows it at boot runs
only in virtual machines upstream. The image lifts that condition.
"""

import re
from pathlib import Path

DROPIN = (Path(__file__).parents[2]
          / "os-image/files/usr/lib/systemd/system/bootc-generic-growpart.service.d"
          / "10-kosheros-bare-metal.conf")


def _directives() -> list[tuple[str, str]]:
    body = DROPIN.read_text().split("[Unit]", 1)[1]
    return re.findall(r"^([A-Za-z]+)=(.*)$", body, re.M)


def test_the_vm_only_condition_is_lifted():
    # An empty assignment resets the upstream list, and nothing puts a
    # virtualization condition back, so the unit runs on bare metal too.
    d = _directives()
    assert ("ConditionVirtualization", "") in d
    assert not any(k == "ConditionVirtualization" and v for k, v in d)


def test_a_family_can_keep_free_space_on_purpose():
    d = _directives()
    assert ("ConditionPathExists", "!/etc/kosher/keep-disk-layout") in d
    # Still only where growpart exists: the same guard upstream had.
    assert ("ConditionPathExists", "/usr/bin/growpart") in d
