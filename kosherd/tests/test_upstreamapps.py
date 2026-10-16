"""A developer's own signed build in the Store (kitty): fetched as the
account, unpacked into its own home only once the signature is the
developer's, and swapped in without losing the copy already there.
"""

from __future__ import annotations

import dataclasses
import io
import json
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest

from kosherd import accountapps, nixapps, upstreamapps as u

ROOT = Path(__file__).parents[2]
KEYS = ROOT / "os-image/files/usr/share/kosher/keys"

pytestmark = pytest.mark.skipif(shutil.which("gpg") is None or shutil.which("gpgv") is None,
                                reason="gnupg not installed")


class _PW:
    def __init__(self, uid, name, home):
        self.pw_uid, self.pw_name, self.pw_dir = uid, name, home


# ---- a developer of our own, with a key --------------------------------------

def _gpg(gnupghome: Path, *args, **kw):
    return subprocess.run(["gpg", "--batch", "--homedir", str(gnupghome), *args],
                          check=True, capture_output=True, **kw)


def _make_key(where: Path, uid: str) -> tuple[Path, str]:
    where.mkdir(parents=True, mode=0o700)
    _gpg(where, "--passphrase", "", "--quick-gen-key", uid, "ed25519", "sign", "never")
    fpr = next(l.split(":")[9] for l in _gpg(where, "--with-colons", "--list-keys", text=True)
               .stdout.splitlines() if l.startswith("fpr:"))
    return where, fpr


@pytest.fixture(scope="module")
def developer(tmp_path_factory):
    base = tmp_path_factory.mktemp("gpg")
    real, real_fpr = _make_key(base / "real", "Developer <dev@example.org>")
    other, other_fpr = _make_key(base / "other", "Someone Else <x@example.org>")
    keys = base / "keys"
    keys.mkdir()
    # The keyring the image ships could, by mistake, carry a second key:
    # only the pinned fingerprint's signature counts.
    ring = keys / "test.gpg"
    ring.write_bytes(_gpg(real, "--export", real_fpr).stdout + _gpg(other, "--export", other_fpr).stdout)
    return {"real": real, "other": other, "fpr": real_fpr, "keys": keys}


def _tarball(version: str) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:xz") as tar:
        def add(name, data, mode=0o644):
            info = tarfile.TarInfo(name)
            info.size, info.mode = len(data), mode
            tar.addfile(info, io.BytesIO(data))
        add("bin/kitty", f"#!/bin/sh\necho kitty {version}\n".encode(), 0o755)
        add("bin/kitten", b"#!/bin/sh\n", 0o755)
        add("share/applications/kitty.desktop",
            b"[Desktop Entry]\nName=kitty\nTryExec=kitty\nExec=kitty\nIcon=kitty\n")
        add("share/icons/hicolor/256x256/apps/kitty.png", b"png")
    return buf.getvalue()


def _sign(gnupghome: Path, data: bytes) -> bytes:
    return _gpg(gnupghome, "--detach-sign", "-o", "-", input=data).stdout


@pytest.fixture
def release(developer, monkeypatch):
    test = dataclasses.replace(u.RELEASES["kitty"], keyring="test.gpg",
                               fingerprint=developer["fpr"])
    monkeypatch.setitem(u.RELEASES, "kitty", test)
    monkeypatch.setattr(u.platform, "machine", lambda: "x86_64")
    return test


def _fetcher(tarball: bytes, signature: bytes):
    asked = []

    def fetch(url, dest, _share):
        asked.append(url)
        dest.write_bytes(signature if url.endswith(".sig") else tarball)
    fetch.asked = asked
    return fetch


def _install(home, developer, version="1.0", signer="real", tamper=False):
    data = _tarball(version)
    sig = _sign(developer[signer], data)
    if tamper:
        data = _tarball(version + "-evil")
    fetch = _fetcher(data, sig)
    u.install_into(home, "kitty", version, fetch=fetch, keys=developer["keys"])
    return fetch


# ---- refs ----------------------------------------------------------------------

def test_only_known_apps_are_fetched():
    assert u.package("upstream#kitty") == "kitty"
    for bad in ("upstream#", "upstream#../x", "upstream#firefox", "nixpkgs#kitty", "kitty"):
        with pytest.raises(u.UpstreamAppError):
            u.package(bad)


def test_the_store_asks_one_place_for_both_kinds_of_per_account_app():
    assert accountapps.kind("upstream#kitty") is u
    assert accountapps.kind("nixpkgs#gh") is nixapps
    assert accountapps.kind("org.mozilla.firefox") is None


# ---- installing ------------------------------------------------------------------

def test_a_signed_build_is_installed_with_launchers_and_commands(tmp_path, developer, release, capsys):
    fetch = _install(tmp_path, developer)
    assert fetch.asked == [
        "https://github.com/kovidgoyal/kitty/releases/download/v1.0/kitty-1.0-x86_64.txz",
        "https://github.com/kovidgoyal/kitty/releases/download/v1.0/kitty-1.0-x86_64.txz.sig"]
    app = tmp_path / ".local/kitty.app"
    assert u.installed_version(tmp_path, "kitty") == "1.0"
    entry = (tmp_path / ".local/share/applications/kitty.desktop").read_text()
    assert f"Exec={app}/bin/kitty\n" in entry
    assert f"TryExec={app}/bin/kitty\n" in entry
    assert f"Icon={app}/share/icons/hicolor/256x256/apps/kitty.png\n" in entry
    assert "X-KosherOS-Upstream=upstream#kitty" in entry
    assert (tmp_path / ".local/bin/kitty").resolve() == app / "bin/kitty"
    # Nothing left beside the install.
    assert sorted(p.name for p in (tmp_path / ".local").iterdir()) == ["bin", "kitty.app", "share"]
    assert json.loads(capsys.readouterr().out.splitlines()[-1]) == {"percent": 100, "status": "Done"}


def test_a_tampered_download_is_refused_and_the_old_copy_stays(tmp_path, developer, release):
    _install(tmp_path, developer, "1.0")
    with pytest.raises(u.UpstreamAppError, match="signature"):
        _install(tmp_path, developer, "2.0", tamper=True)
    assert u.installed_version(tmp_path, "kitty") == "1.0"
    assert "kitty 1.0" in (tmp_path / ".local/kitty.app/bin/kitty").read_text()


def test_a_signature_by_any_other_key_is_refused(tmp_path, developer, release):
    with pytest.raises(u.UpstreamAppError, match="signature"):
        _install(tmp_path, developer, signer="other")
    assert not (tmp_path / ".local/kitty.app").exists()


def test_an_upgrade_replaces_the_copy(tmp_path, developer, release):
    _install(tmp_path, developer, "1.0")
    _install(tmp_path, developer, "2.0")
    assert u.installed_version(tmp_path, "kitty") == "2.0"
    assert "kitty 2.0" in (tmp_path / ".local/kitty.app/bin/kitty").read_text()


def test_a_persons_own_files_are_left_alone_and_removal_takes_only_ours(tmp_path, developer, release):
    mine = tmp_path / ".local/share/applications/kitty.desktop"
    mine.parent.mkdir(parents=True)
    mine.write_text("[Desktop Entry]\nName=my kitty\n")
    _install(tmp_path, developer)
    assert mine.read_text() == "[Desktop Entry]\nName=my kitty\n"
    u.unlink(tmp_path, "kitty")
    assert mine.exists()
    assert not (tmp_path / ".local/bin/kitty").is_symlink()


def test_remove_and_up_to_date_through_the_command(tmp_path, developer, release, monkeypatch):
    _install(tmp_path, developer, "1.0")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(u, "latest_version", lambda _r: "1.0")
    assert u.main(["upgrade", "kitty"]) == 0
    assert u.installed_version(tmp_path, "kitty") == "1.0"
    assert u.main(["remove", "kitty"]) == 0
    assert not (tmp_path / ".local/kitty.app").exists()
    assert not (tmp_path / ".local/share/applications/kitty.desktop").exists()
    assert u.main(["install", "firefox"]) == 2


def test_a_failure_is_said_in_one_line(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))

    def offline(_r):
        raise u.urllib.error.URLError("no route")
    monkeypatch.setattr(u, "latest_version", offline)
    assert u.main(["install", "kitty"]) == 1
    said = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert said["error"].startswith("could not reach the developer's site")


# ---- kosherd's side ---------------------------------------------------------------

def test_the_helper_runs_as_the_account_with_a_clean_environment(monkeypatch, tmp_path):
    monkeypatch.setattr(u.pwd, "getpwuid", lambda uid: _PW(uid, f"u{uid}", str(tmp_path)))
    argv = u._argv(1001, "install", "kitty")
    assert argv[:6] == ["runuser", "-u", "u1001", "--", "env", "-i"]
    assert f"HOME={tmp_path}" in argv
    assert argv[-3:] == ["kosher-upstream-app", "install", "kitty"]


def test_installing_kitty_takes_away_the_nix_build_that_never_opened(monkeypatch, tmp_path):
    monkeypatch.setattr(u.pwd, "getpwuid", lambda uid: _PW(uid, f"u{uid}", str(tmp_path)))
    monkeypatch.setattr(u, "_run", lambda *a: None)
    monkeypatch.setattr(nixapps, "installed", lambda uid: {"nixpkgs#kitty"})
    removed = []
    monkeypatch.setattr(nixapps, "remove", lambda ref, uid, p: removed.append((ref, uid)))
    u.install("upstream#kitty", 1001, lambda *a: None)
    assert removed == [("nixpkgs#kitty", 1001)]


def test_progress_and_the_reason_reach_the_store(monkeypatch):
    class Proc:
        stdout = io.StringIO('{"percent": 40, "status": "Downloading\\u2026"}\nnoise\n'
                             '{"error": "the download\'s signature did not check out"}\n')
        stderr = io.StringIO("")
        def wait(self): return 1
    monkeypatch.setattr(u.subprocess, "Popen", lambda *a, **k: Proc())
    seen = []
    with pytest.raises(u.UpstreamAppError, match="signature did not check out"):
        u._run("upstream#kitty", ["x"], lambda *a: seen.append(a))
    assert seen == [("upstream#kitty", 40, "Downloading…")]


# ---- the image ----------------------------------------------------------------------

def test_the_image_carries_kittys_key_and_the_pin_is_that_key():
    ring = KEYS / u.RELEASES["kitty"].keyring
    out = subprocess.run(["gpg", "--batch", "--with-colons", "--show-keys", str(ring)],
                         capture_output=True, text=True, check=True).stdout
    fprs = [l.split(":")[9] for l in out.splitlines() if l.startswith("fpr:")]
    assert fprs[0] == u.RELEASES["kitty"].fingerprint


def test_the_catalog_offers_kittys_own_build_and_the_command_is_installed():
    catalog = json.loads((ROOT / "os-image/files/etc/kosher/catalog.json").read_text())
    refs = {a["ref"] for a in catalog["apps"]}
    assert "upstream#kitty" in refs and "nixpkgs#kitty" not in refs
    for a in catalog["apps"]:
        if u.is_upstream(a["ref"]):
            u.package(a["ref"])
    assert 'kosher-upstream-app = "kosherd.upstreamapps:main"' in (ROOT / "kosherd/pyproject.toml").read_text()
