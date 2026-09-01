"""The machine has to introduce itself by name.

`echo kosheros > /etc/hostname` in a Containerfile RUN does nothing: the
container engine bind-mounts that file over the build container, so the
write lands on a temporary file and the image ships an empty one. systemd
then falls back to DEFAULT_HOSTNAME, which Fedora sets to "fedora", and
every login prompt on the machine said "fedora login:".

The same bind mount means a RUN cannot verify the fix either — it cannot
read what COPY put in the layer. So the file is checked here, against the
build context, where nothing is mounted over anything.
"""

from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
FILES = ROOT / "os-image/files"
CONTAINERFILE = ROOT / "os-image/Containerfile"


def test_the_hostname_is_shipped_as_a_file():
    hostname = FILES / "etc/hostname"
    assert hostname.exists(), "no /etc/hostname in the image files"
    assert hostname.read_text().strip() == "kosheros"


def test_the_hostname_is_copied_and_not_written_in_a_run():
    # A RUN that writes it looks right, passes review, and does nothing.
    body = CONTAINERFILE.read_text()
    for line in body.splitlines():
        if line.startswith("RUN") or line.strip().startswith("&&"):
            assert "> /etc/hostname" not in line, (
                "writing /etc/hostname in a RUN has no effect: the engine "
                "bind-mounts it over the build container. COPY it instead.")


def test_the_fallback_hostname_is_ours_too():
    # systemd uses DEFAULT_HOSTNAME whenever /etc/hostname is absent or
    # empty, which is precisely the state that caused this.
    body = CONTAINERFILE.read_text()
    assert "DEFAULT_HOSTNAME=kosheros" in body


def test_the_other_files_the_engine_mounts_are_not_written_either():
    # Same trap, same silence: /etc/hosts and /etc/resolv.conf are also
    # bind-mounted during a build.
    body = CONTAINERFILE.read_text()
    for path in ("/etc/hosts", "/etc/resolv.conf"):
        for line in body.splitlines():
            if line.startswith("RUN") or line.strip().startswith("&&"):
                assert f"> {path}" not in line, (
                    f"writing {path} in a RUN has no effect; COPY it")
