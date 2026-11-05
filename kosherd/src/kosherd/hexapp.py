"""Hex, Anomaly's voice dictation, from Anomaly's own signed build.

Anomaly ships Hex for Linux as one executable. Its release service
answers a manifest (https://anoma.ly/release/hex/desktop/stable.json)
naming each build with its size and SHA-256, and a 64-byte ed25519
signature over "anomaly.hex.linux-artifact.v1\\n<sha256>\\n" by Anomaly's
Linux release key. The .deb and .rpm do not fit an image-based Fedora;
the executable does.

This lays Hex out exactly as Anomaly's own installer does
(https://anoma.ly/hex/install-linux.sh), into the account's home:

    ~/.local/share/hex/versions/<version>/hex
    ~/.local/share/hex/current -> versions/<version>
    ~/.local/share/hex/install.json     (lets Hex update itself)
    ~/.local/bin/hex -> ~/.local/share/hex/current/hex
    ~/.local/share/applications/anomaly.hex.desktop
    ~/.config/autostart/anomaly.hex.desktop
    ~/.config/systemd/user/hex.service

Keeping that layout matters: Hex reads install.json to update itself
(through the same release service and the same signature check), its
"Launch at Login" switch edits that autostart entry, and its own
uninstaller recognises every file here. So the Store and Hex agree on
what is installed: the version is wherever `current` points, whoever
moved it.

Runs as the account (kosher-upstream-app), never as root.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Callable

from .upstreamapps import UpstreamAppError

ORIGIN = "https://anoma.ly"
MANIFEST = ORIGIN + "/release/hex/desktop/stable.json"
# Anomaly's ed25519 Linux release key, as install-linux.sh pins it
# (.github/release/linux-signing.pub in anomalyco/hex).
SIGNING_KEY = "MCowBQYDK2VwAyEAv60C5iII/xRLXJ0hx+ecfBbGkEKZpDfYVzAwB81P99g="
SIGNED_PREFIX = b"anomaly.hex.linux-artifact.v1\n"
TARGETS = {"x86_64": "linux-x64", "aarch64": "linux-arm64"}
MAX_BYTES = 512 << 20
MANAGED = "X-Hex-Managed=true"
_FILE_URL = re.compile(r"/([A-Za-z0-9._-]+/)*hex/files/[0-9a-f]{64}/[A-Za-z0-9._-]+")
_VERSION = re.compile(r"[0-9A-Za-z][0-9A-Za-z.+_-]{0,127}")


class HexError(UpstreamAppError):
    pass


Say = Callable[..., None]
Fetch = Callable[[str, Path, "tuple[int, int] | None"], None]


def support(home: Path) -> Path:
    return home / ".local/share/hex"


def installed_version(home: Path) -> str | None:
    """Where `current` points: Hex's own updater moves it too."""
    try:
        target = os.readlink(support(home) / "current")
    except OSError:
        return None
    version = target.removeprefix("versions/")
    if target == version or not _VERSION.fullmatch(version):
        return None
    return version if (support(home) / "versions" / version / "hex").is_file() else None


# ---- the release service -----------------------------------------------------

def parse_manifest(data: bytes, machine: str | None = None) -> dict:
    """The parts of stable.json that matter, checked the way
    install-linux.sh checks them."""
    try:
        m = json.loads(data)
    except ValueError as e:
        raise HexError("Anomaly's release service answered something that is not a manifest") from e
    if not isinstance(m, dict) or (m.get("schema"), m.get("app"), m.get("artifact"), m.get("channel")) \
            != (1, "hex", "desktop", "stable"):
        raise HexError("Anomaly's release service answered another manifest than Hex's")
    target = TARGETS.get(machine or platform.machine())
    if target is None:
        raise HexError("Hex has no build for this kind of computer")
    release = m.get("release") or {}
    version = str(release.get("version") or "")
    if not _VERSION.fullmatch(version):
        raise HexError("Hex's release has no usable version")
    targets = release.get("targets") or {}
    exe, sig = targets.get(target), targets.get(target + "-sig")
    if not isinstance(exe, dict) or not isinstance(sig, dict):
        raise HexError(f"Hex {version} has no signed build for this computer")
    for t in (exe, sig):
        if not _FILE_URL.fullmatch(str(t.get("url") or "")):
            raise HexError("Hex's manifest names an unexpected file")
        if not re.fullmatch(r"[0-9a-f]{64}", str(t.get("sha256") or "")):
            raise HexError("Hex's manifest has an invalid checksum")
    size = exe.get("size")
    if not isinstance(size, int) or not 0 < size <= MAX_BYTES or sig.get("size") != 64:
        raise HexError("Hex's manifest has an invalid size")
    return {"version": version, "url": ORIGIN + exe["url"], "size": size, "sha256": exe["sha256"],
            "sig_url": ORIGIN + sig["url"], "sig_sha256": sig["sha256"]}


def latest(read: Callable[[str], bytes]) -> dict:
    return parse_manifest(read(MANIFEST))


def verify(sha256: str, signature: bytes, key: str = SIGNING_KEY) -> None:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    from cryptography.hazmat.primitives.serialization import load_der_public_key

    public = load_der_public_key(base64.b64decode(key))
    assert isinstance(public, Ed25519PublicKey)
    try:
        public.verify(signature, SIGNED_PREFIX + sha256.encode() + b"\n")
    except InvalidSignature:
        raise HexError("the download's signature is not Anomaly's, so it was not installed") from None


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


# ---- laying it out -----------------------------------------------------------

def _desktop(binary: Path) -> str:
    return (
        "[Desktop Entry]\nType=Application\nName=Hex\nComment=Local voice dictation\n"
        f'Exec="{binary}"\nIcon=anomaly.hex\nTerminal=false\nCategories=Utility;Accessibility;\n'
        "StartupNotify=true\nStartupWMClass=anomaly.hex\nActions=quit;\n"
        f"{MANAGED}\nX-KosherOS-Upstream=upstream#hex\n\n"
        f'[Desktop Action quit]\nName=Quit Hex\nExec="{binary}" quit\n')


def _autostart(binary: Path) -> str:
    return (
        "[Desktop Entry]\nType=Application\nName=Hex\nComment=Local voice dictation\n"
        f'Exec="{binary}" start\nIcon=anomaly.hex\nTerminal=false\n'
        f"X-GNOME-Autostart-enabled=true\n{MANAGED}\n")


def _unit(binary: Path, root: Path) -> str:
    b = str(binary).replace("%", "%%").replace("$", "$$")
    r = str(root).replace("%", "%%")
    return (
        f"# {MANAGED}\n[Unit]\nDescription=Hex background dictation\n"
        "PartOf=graphical-session.target\nAfter=graphical-session.target\n"
        "StartLimitIntervalSec=60\nStartLimitBurst=5\n\n"
        "[Service]\nType=notify\nNotifyAccess=main\n"
        f'ExecStart="{b}" service\nEnvironment="HEX_APPLICATION_SUPPORT_DIR={r}"\n'
        f"EnvironmentFile=-{r}/session.env\nRestart=on-failure\nRestartSec=3\n"
        "TimeoutStopSec=30\nUMask=0077\n\n[Install]\nWantedBy=graphical-session.target\n")


def _hex_entry(path: Path) -> bool:
    try:
        lines = path.read_text().splitlines()
    except (OSError, UnicodeDecodeError):
        return False
    return MANAGED in lines or "X-XDP-Autostart=anomaly.hex" in lines or f"# {MANAGED}" in lines


def _ours_or_absent(path: Path) -> bool:
    if path.is_symlink():
        return False
    return not path.exists() or _hex_entry(path)


def _replace(path: Path, text: str, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.kosheros")
    tmp.write_text(text)
    tmp.chmod(mode)
    os.replace(tmp, path)


def _systemctl(*args: str) -> None:
    """Best effort: from the Store there may be no session bus at all."""
    if shutil.which("systemctl") and os.environ.get("XDG_RUNTIME_DIR"):
        subprocess.run(["systemctl", "--user", *args], capture_output=True, timeout=30)


def _private_dir(path: Path) -> None:
    if path.is_symlink():
        raise HexError(f"{path} is a link; Hex was not installed over it")
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(0o700)


def install_into(home: Path, release: dict, fetch: Fetch, say: Say,
                 key: str = SIGNING_KEY, check_runs: bool = True) -> None:
    """Download beside the installed copy, check size, checksum and
    Anomaly's signature, and only then move `current`. A failure leaves
    whatever was installed as it was."""
    root = support(home)
    version = release["version"]
    _private_dir(root)
    _private_dir(root / "versions")
    _private_dir(root / "updates")
    binary = home / ".local/bin/hex"
    expected = root / "current/hex"
    if (binary.exists() or binary.is_symlink()) and \
            not (binary.is_symlink() and os.readlink(binary) == str(expected)):
        raise HexError(f"{binary} is not Hex's; it was left alone and Hex was not installed")
    with tempfile.TemporaryDirectory(prefix=".hex-", dir=root / "updates") as tmp:
        exe, sig = Path(tmp) / "hex", Path(tmp) / "hex.sig"
        fetch(release["url"], exe, (2, 90))
        fetch(release["sig_url"], sig, None)
        say(percent=92, status="Checking…")
        if exe.stat().st_size != release["size"] or _sha256(exe) != release["sha256"]:
            raise HexError("the download was not the file Anomaly published, so it was not installed")
        if sig.stat().st_size != 64 or _sha256(sig) != release["sig_sha256"]:
            raise HexError("the download's signature is damaged, so it was not installed")
        verify(release["sha256"], sig.read_bytes(), key)
        exe.chmod(0o755)
        if check_runs:
            _check_runs(exe, version)
        say(percent=95, status="Installing…")
        vdir = root / "versions" / version
        _private_dir(vdir)
        os.replace(exe, vdir / "hex")
    nxt = root / ".current.next"
    nxt.unlink(missing_ok=True)
    nxt.symlink_to(f"versions/{version}")
    os.replace(nxt, root / "current")
    (root / "versions" / version / "source-build").unlink(missing_ok=True)
    _replace(root / "install.json", json.dumps(
        {"app": "hex", "artifact": "desktop", "channel": "stable", "method": "direct",
         "root": str(root)}, separators=(",", ":")) + "\n", 0o600)
    binary.parent.mkdir(parents=True, exist_ok=True)
    if not binary.is_symlink():
        binary.symlink_to(expected)
    _launchers(home, binary, root)
    _prune(root, keep=version)
    say(percent=100, status="Done")


def _launchers(home: Path, binary: Path, root: Path) -> None:
    files = {
        home / ".local/share/applications/anomaly.hex.desktop": _desktop(binary),
        home / ".config/autostart/anomaly.hex.desktop": _autostart(binary),
        home / ".config/systemd/user/hex.service": _unit(binary, root),
    }
    for path, text in files.items():
        if path == home / ".config/autostart/anomaly.hex.desktop" and path.exists() \
                and _hex_entry(path):
            # Hex's Launch at Login switch owns this one after the first
            # install: an upgrade must not turn it back on.
            continue
        if _ours_or_absent(path):
            _replace(path, text)
    _systemctl("daemon-reload")


def _check_runs(exe: Path, version: str) -> None:
    """The executable answers its version, so a library the image lacks
    shows up here and not as an app that never opens."""
    try:
        out = subprocess.run([str(exe), "--version"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise HexError(f"Hex {version} did not start on this computer: {e}") from None
    words = out.stdout.split()
    if out.returncode != 0 or not words or words[-1] != version:
        missing = [l.split()[0] for l in _ldd(exe) if "not found" in l]
        reason = f"it needs {', '.join(missing)}" if missing else (out.stderr.strip()[-200:] or "no reason given")
        raise HexError(f"Hex {version} did not start on this computer: {reason}")


def _ldd(exe: Path) -> list[str]:
    try:
        return subprocess.run(["ldd", str(exe)], capture_output=True, text=True, timeout=30).stdout.splitlines()
    except (OSError, subprocess.TimeoutExpired):
        return []


def _prune(root: Path, keep: str) -> None:
    """Old versions are 70 MB each; Hex rolls back to none of them."""
    for d in (root / "versions").iterdir():
        if d.name != keep and d.is_dir() and not d.is_symlink():
            shutil.rmtree(d, ignore_errors=True)


def remove(home: Path) -> None:
    """What `install-linux.sh uninstall` removes: the program and its
    launchers. Settings, history and downloaded models stay, as they do
    with Anomaly's uninstaller, so reinstalling picks up where it was."""
    root = support(home)
    if (home / ".local/bin/hex").exists():
        try:
            subprocess.run([str(home / ".local/bin/hex"), "quit"], capture_output=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            pass
    unit = home / ".config/systemd/user/hex.service"
    if unit.is_file() and not unit.is_symlink() and _hex_entry(unit):
        _systemctl("disable", "--now", "hex.service")
        unit.unlink()
        _systemctl("daemon-reload")
    binary = home / ".local/bin/hex"
    if binary.is_symlink() and os.readlink(binary) == str(root / "current/hex"):
        binary.unlink()
    for d in (home / ".local/share/applications", home / ".config/autostart"):
        for name in ("anomaly.hex.desktop", "hex.desktop"):
            p = d / name
            if p.is_file() and not p.is_symlink() and _hex_entry(p):
                p.unlink()
    for name in ("current", ".current.next", "install.json"):
        p = root / name
        if p.is_symlink() or p.is_file():
            p.unlink()
    if (root / "versions").is_dir() and not (root / "versions").is_symlink():
        shutil.rmtree(root / "versions", ignore_errors=True)
    # The icons Hex writes into the account's theme when nothing else
    # ships them, marked inside the PNG, as Anomaly's uninstaller finds them.
    for icon in (home / ".local/share/icons/hicolor").glob("*/apps/anomaly.hex.png"):
        try:
            if not icon.is_symlink() and MANAGED.encode() in icon.read_bytes():
                icon.unlink()
        except OSError:
            pass
