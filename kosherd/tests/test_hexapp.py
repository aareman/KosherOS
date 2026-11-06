"""Hex from Anomaly's signed build: checked against Anomaly's key before
anything the account has is touched, and laid out the way Anomaly's own
installer does so Hex can update and uninstall itself.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from kosherd import hexapp, upstreamapps as u

ROOT = Path(__file__).parents[2]


def _key():
    private = Ed25519PrivateKey.generate()
    der = private.public_key().public_bytes(Encoding.DER, PublicFormat.SubjectPublicKeyInfo)
    return private, base64.b64encode(der).decode()


def _exe(version: str) -> bytes:
    return f"#!/bin/sh\necho hex {version}\n".encode()


def _manifest(version: str, exe: bytes, sig: bytes, target: str = "linux-x64") -> bytes:
    def entry(data, name):
        h = hashlib.sha256(data).hexdigest()
        return {"url": f"/release/hex/files/{h}/{name}", "size": len(data), "sha256": h}
    return json.dumps({"schema": 1, "app": "hex", "artifact": "desktop", "channel": "stable",
                       "release": {"version": version, "targets": {
                           target: entry(exe, "hex"), f"{target}-sig": entry(sig, "hex.sig")}}}).encode()


class Developer:
    """Anomaly's release service, in memory."""

    def __init__(self):
        self.private, self.key = _key()
        self.files: dict[str, bytes] = {}

    def publish(self, version: str, exe: bytes | None = None, signer=None) -> dict:
        exe = exe if exe is not None else _exe(version)
        digest = hashlib.sha256(exe).hexdigest()
        sig = (signer or self.private).sign(hexapp.SIGNED_PREFIX + digest.encode() + b"\n")
        release = hexapp.parse_manifest(_manifest(version, exe, sig), "x86_64")
        self.files[release["url"]] = exe
        self.files[release["sig_url"]] = sig
        return release

    def fetch(self, url, dest, share):
        dest.write_bytes(self.files[url])


def _install(home, dev, release):
    hexapp.install_into(home, release, dev.fetch, lambda **_: None, key=dev.key, check_runs=False)


@pytest.fixture
def dev():
    return Developer()


@pytest.fixture(autouse=True)
def systemd(monkeypatch):
    """The account's user manager is the one running the tests' own
    session; a test reaching it once stopped and disabled the developer's
    real Hex. What systemd does with these is checked in the VM
    (scripts/vm-tests/30-apps.sh)."""
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(hexapp, "_systemctl", lambda *args: calls.append(args))
    return calls


def test_a_signed_build_is_laid_out_as_anomalys_installer_lays_it(tmp_path, dev):
    _install(tmp_path, dev, dev.publish("3.3.0"))
    root = tmp_path / ".local/share/hex"
    assert os.readlink(root / "current") == "versions/3.3.0"
    assert (root / "versions/3.3.0/hex").read_bytes() == _exe("3.3.0")
    assert os.access(root / "versions/3.3.0/hex", os.X_OK)
    assert os.readlink(tmp_path / ".local/bin/hex") == str(root / "current/hex")
    assert json.loads((root / "install.json").read_text()) == {
        "app": "hex", "artifact": "desktop", "channel": "stable", "method": "direct", "root": str(root)}
    assert (root.stat().st_mode & 0o777) == 0o700
    desktop = (tmp_path / ".local/share/applications/anomaly.hex.desktop").read_text()
    assert f'Exec="{tmp_path}/.local/bin/hex"' in desktop and "X-Hex-Managed=true" in desktop
    assert "start" in (tmp_path / ".config/autostart/anomaly.hex.desktop").read_text()
    unit = (tmp_path / ".config/systemd/user/hex.service").read_text()
    assert unit.startswith("# X-Hex-Managed=true") and f'"{tmp_path}/.local/bin/hex" service' in unit
    assert hexapp.installed_version(tmp_path) == "3.3.0"
    assert u.installed_version(tmp_path, "hex") == "3.3.0"


def test_a_build_not_signed_by_anomaly_is_refused_and_the_old_copy_stays(tmp_path, dev):
    _install(tmp_path, dev, dev.publish("1.0"))
    stranger, _ = _key()
    with pytest.raises(hexapp.HexError, match="not Anomaly's"):
        _install(tmp_path, dev, dev.publish("2.0", signer=stranger))
    assert hexapp.installed_version(tmp_path) == "1.0"
    assert not (tmp_path / ".local/share/hex/versions/2.0").exists()


def test_a_download_that_is_not_the_published_file_is_refused(tmp_path, dev):
    release = dev.publish("1.0")
    dev.files[release["url"]] = b"#!/bin/sh\necho something else\n"
    with pytest.raises(hexapp.HexError, match="not the file Anomaly published"):
        _install(tmp_path, dev, release)
    assert hexapp.installed_version(tmp_path) is None
    assert not (tmp_path / ".local/bin/hex").exists()


def test_an_upgrade_moves_current_keeps_the_login_choice_and_drops_the_old_version(tmp_path, dev):
    _install(tmp_path, dev, dev.publish("1.0"))
    autostart = tmp_path / ".config/autostart/anomaly.hex.desktop"
    # The person turned Launch at Login off in Hex, which edits this entry.
    autostart.write_text(autostart.read_text().replace("enabled=true", "enabled=false"))
    _install(tmp_path, dev, dev.publish("1.1"))
    assert hexapp.installed_version(tmp_path) == "1.1"
    assert "enabled=false" in autostart.read_text()
    assert not (tmp_path / ".local/share/hex/versions/1.0").exists()


def test_hex_updating_itself_is_what_the_store_sees(tmp_path, dev):
    _install(tmp_path, dev, dev.publish("1.0"))
    root = tmp_path / ".local/share/hex"
    (root / "versions/1.1").mkdir()
    (root / "versions/1.1/hex").write_bytes(_exe("1.1"))
    (root / "current").unlink()
    (root / "current").symlink_to("versions/1.1")
    assert hexapp.installed_version(tmp_path) == "1.1"


def test_files_that_are_not_hexs_are_left_alone(tmp_path, dev):
    mine = tmp_path / ".local/bin/hex"
    mine.parent.mkdir(parents=True)
    mine.write_text("#!/bin/sh\necho my own hex tool\n")
    with pytest.raises(hexapp.HexError, match="not Hex's"):
        _install(tmp_path, dev, dev.publish("1.0"))
    assert "my own" in mine.read_text()

    mine.unlink()
    unit = tmp_path / ".config/systemd/user/hex.service"
    unit.parent.mkdir(parents=True)
    unit.write_text("[Service]\nExecStart=/opt/other\n")
    _install(tmp_path, dev, dev.publish("1.0"))
    assert unit.read_text() == "[Service]\nExecStart=/opt/other\n"


def test_removal_takes_the_program_and_keeps_settings_and_models(tmp_path, dev, systemd):
    _install(tmp_path, dev, dev.publish("1.0"))
    root = tmp_path / ".local/share/hex"
    (root / "settings.json").write_text("{}")
    (root / "models").mkdir()
    icons = tmp_path / ".local/share/icons/hicolor"
    (icons / "64x64/apps").mkdir(parents=True)
    (icons / "64x64/apps/anomaly.hex.png").write_bytes(b"\x89PNG tEXt X-Hex-Managed=true")
    (icons / "64x64/apps/other.png").write_bytes(b"\x89PNG")
    (icons / "48x48/apps").mkdir(parents=True)
    (icons / "48x48/apps/anomaly.hex.png").write_bytes(b"\x89PNG from someone else")
    hexapp.remove(tmp_path)
    assert not (icons / "64x64/apps/anomaly.hex.png").exists()
    assert (icons / "64x64/apps/other.png").exists() and (icons / "48x48/apps/anomaly.hex.png").exists()
    assert hexapp.installed_version(tmp_path) is None
    assert not (tmp_path / ".local/bin/hex").is_symlink()
    assert not (root / "versions").exists() and not (root / "install.json").exists()
    assert not (tmp_path / ".local/share/applications/anomaly.hex.desktop").exists()
    assert not (tmp_path / ".config/autostart/anomaly.hex.desktop").exists()
    assert not (tmp_path / ".config/systemd/user/hex.service").exists()
    assert (root / "settings.json").exists() and (root / "models").is_dir()
    assert systemd[-2:] == [("disable", "--now", "hex.service"), ("daemon-reload",)]


@pytest.mark.parametrize("change, message", [
    (lambda m: m.update(app="other"), "another manifest"),
    (lambda m: m["release"].update(version="../x"), "no usable version"),
    (lambda m: m["release"]["targets"]["linux-x64"].update(url="https://elsewhere.example/hex"),
     "unexpected file"),
    (lambda m: m["release"]["targets"]["linux-x64"].update(size=1 << 40), "invalid size"),
    (lambda m: m["release"]["targets"].pop("linux-x64-sig"), "no signed build"),
])
def test_a_manifest_that_does_not_add_up_is_refused(change, message):
    m = json.loads(_manifest("1.0", _exe("1.0"), b"s" * 64))
    change(m)
    with pytest.raises(hexapp.HexError, match=message):
        hexapp.parse_manifest(json.dumps(m).encode(), "x86_64")


def test_only_anomalys_host_is_fetched_from():
    release = hexapp.parse_manifest(_manifest("1.0", _exe("1.0"), b"s" * 64), "x86_64")
    assert release["url"].startswith("https://anoma.ly/release/hex/files/")
    assert release["sig_url"].startswith("https://anoma.ly/release/hex/files/")
    with pytest.raises(hexapp.HexError, match="no build for this kind"):
        hexapp.parse_manifest(_manifest("1.0", _exe("1.0"), b"s" * 64), "riscv64")


def test_the_pinned_key_is_anomalys_published_one():
    # As https://anoma.ly/hex/install-linux.sh pins it.
    assert hexapp.SIGNING_KEY == "MCowBQYDK2VwAyEAv60C5iII/xRLXJ0hx+ecfBbGkEKZpDfYVzAwB81P99g="


def test_the_store_knows_hex_and_the_catalog_offers_it():
    assert u.package("upstream#hex") == "hex"
    catalog = json.loads((ROOT / "os-image/files/etc/kosher/catalog.json").read_text())
    entry = next(a for a in catalog["apps"] if a["ref"] == "upstream#hex")
    assert entry["name"] == "Hex" and entry["developer"] == "Anomaly"


def test_the_helper_dispatches_hex(monkeypatch, tmp_path, dev):
    release = dev.publish("1.0")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(hexapp, "latest", lambda read: release)
    monkeypatch.setattr(u, "_download", dev.fetch)
    monkeypatch.setattr(hexapp, "SIGNING_KEY", dev.key)
    real = hexapp.install_into
    monkeypatch.setattr(hexapp, "install_into",
                        lambda home, rel, fetch, say: real(home, rel, fetch, say, key=dev.key,
                                                           check_runs=False))
    assert u.main(["install", "hex"]) == 0
    assert hexapp.installed_version(tmp_path) == "1.0"
    assert u.main(["upgrade", "hex"]) == 0
    assert u.main(["remove", "hex"]) == 0
    assert hexapp.installed_version(tmp_path) is None
