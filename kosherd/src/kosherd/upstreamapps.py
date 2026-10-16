"""Apps from their developer's own signed build: the third kind of app the
Store carries.

Some apps are packaged in nixpkgs but do not run from there. A Nix build
links its own libGL, which finds no graphics driver on Fedora, so a
terminal that draws with OpenGL refuses to open even on a machine whose
drivers are fine: kitty says "kitty requires working OpenGL 3.1 drivers".
The developer's own build links nothing of the kind and uses the
machine's drivers, so for those apps the Store installs that instead.

A catalog entry whose ref is `upstream#<name>` names one of RELEASES
below. Like a Nix app it goes into the asking account's OWN home and is
fetched AS THAT ACCOUNT, so the download passes through that account's
filter, and nobody else's PATH or app grid sees it. The tarball is only
unpacked after its detached signature checks out against the developer's
key, which the image carries; a download that fails the check is thrown
away and whatever was installed before stays.

Two halves: the functions kosherd calls (as root), and `main`, the
`kosher-upstream-app` command they run as the account through runuser.
"""

from __future__ import annotations

import json
import logging
import os
import platform
import pwd
import shutil
import ssl
import subprocess
import sys
import tarfile
import tempfile
import urllib.error
import urllib.request
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)

PREFIX = "upstream#"
CA_BUNDLE = "/etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem"
KEYS = Path("/usr/share/kosher/keys")
MARK = "X-KosherOS-Upstream"
VERSION_FILE = ".kosheros-version"


class UpstreamAppError(Exception):
    pass


@dataclass(frozen=True)
class Release:
    """Where one app's builds are and how to tell they are genuine."""
    version_url: str        # answers the current version, nothing else
    tarball: str            # {version} and {arch} filled in
    keyring: str            # file name under KEYS
    fingerprint: str        # the only key whose signature is accepted
    directory: str          # under the account's home
    binaries: tuple[str, ...]
    desktop_files: tuple[str, ...]
    icon: str               # inside the unpacked directory
    arches: dict            # platform.machine() -> the name in the tarball
    replaces: str = ""      # the catalog's earlier ref for the same app


RELEASES = {
    # https://sw.kovidgoyal.net/kitty/binary/ — the installer script there
    # does what install() does, minus the signature check.
    "kitty": Release(
        version_url="https://sw.kovidgoyal.net/kitty/current-version.txt",
        tarball="https://github.com/kovidgoyal/kitty/releases/download/"
                "v{version}/kitty-{version}-{arch}.txz",
        keyring="kitty-kovid-goyal.gpg",
        fingerprint="3CE1780F78DD88DF45194FD706BC317B515ACE7C",
        directory=".local/kitty.app",
        binaries=("kitty", "kitten"),
        desktop_files=("kitty.desktop", "kitty-open.desktop"),
        icon="share/icons/hicolor/256x256/apps/kitty.png",
        arches={"x86_64": "x86_64", "aarch64": "arm64"},
        replaces="nixpkgs#kitty",
    ),
}


def is_upstream(ref: str) -> bool:
    return isinstance(ref, str) and ref.startswith(PREFIX)


def package(ref: str) -> str:
    """'upstream#kitty' -> 'kitty'. Only names in RELEASES pass: the name
    picks URLs and a key, so nothing else may reach that far."""
    name = ref[len(PREFIX):] if is_upstream(ref) else ""
    if name not in RELEASES:
        raise UpstreamAppError(f"{ref!r} is not an app KosherOS knows how to fetch")
    return name


def _home(uid: int) -> Path:
    return Path(pwd.getpwuid(uid).pw_dir)


def installed_version(home: Path, name: str) -> str | None:
    try:
        return (home / RELEASES[name].directory / VERSION_FILE).read_text().strip() or None
    except OSError:
        return None


def installed(uid: int) -> set[str]:
    """No network and no subprocess: asked on every Store open."""
    try:
        home = _home(uid)
    except KeyError:
        return set()
    return {PREFIX + n for n in RELEASES if installed_version(home, n)}


# ---- kosherd's side: run the helper as the account ---------------------------

def _argv(uid: int, *args: str) -> list[str]:
    pw = pwd.getpwuid(uid)
    return [
        "runuser", "-u", pw.pw_name, "--",
        "env", "-i",
        f"HOME={pw.pw_dir}", f"USER={pw.pw_name}", f"LOGNAME={pw.pw_name}",
        "PATH=/usr/bin:/bin",
        f"SSL_CERT_FILE={CA_BUNDLE}",
        "kosher-upstream-app", *args,
    ]


def _run(ref: str, argv: list[str], on_progress: Callable[[str, int, str], None]) -> None:
    """The helper prints one JSON object per line on stdout: progress as
    {"percent", "status"}, a failure as {"error"}."""
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    error = None
    assert proc.stdout is not None
    for raw in proc.stdout:
        try:
            msg = json.loads(raw)
        except ValueError:
            continue
        if "error" in msg:
            error = str(msg["error"])
        else:
            on_progress(ref, int(msg.get("percent", 0)), str(msg.get("status", "Working…")))
    stderr = proc.stderr.read() if proc.stderr else ""
    code = proc.wait()
    if code != 0:
        tail: deque[str] = deque((l for l in stderr.splitlines() if l.strip()), maxlen=1)
        raise UpstreamAppError(error or (tail[0] if tail else f"the installer exited {code}"))


def _drop_replaced(name: str, uid: int, on_progress) -> None:
    """The broken build this one replaces, if the account still has it:
    two kittys in the app grid, one of which never opens, helps nobody."""
    from . import nixapps

    old = RELEASES[name].replaces
    if old and nixapps.is_nix(old) and old in nixapps.installed(uid):
        try:
            nixapps.remove(old, uid, on_progress)
        except nixapps.NixAppError as e:
            log.warning("could not remove %s for uid %d: %s", old, uid, e)


def install(ref: str, uid: int, on_progress) -> None:
    name = package(ref)
    on_progress(ref, 0, "Looking up…")
    _run(ref, _argv(uid, "install", name), on_progress)
    _drop_replaced(name, uid, on_progress)


def upgrade(ref: str, uid: int, on_progress) -> None:
    name = package(ref)
    on_progress(ref, 0, "Updating…")
    _run(ref, _argv(uid, "upgrade", name), on_progress)


def remove(ref: str, uid: int, on_progress) -> None:
    name = package(ref)
    on_progress(ref, 0, "Removing…")
    _run(ref, _argv(uid, "remove", name), on_progress)


def exists(ref: str) -> bool | None:
    """Whether the developer still answers a current version: for
    `kosherctl check-catalog`. None when nothing can be said (offline)."""
    try:
        return bool(latest_version(RELEASES[package(ref)]))
    except UpstreamAppError:
        return False
    except OSError:
        return None


# ---- the account's side: kosher-upstream-app ---------------------------------

def _say(**msg) -> None:
    print(json.dumps(msg), flush=True)


def _context() -> ssl.SSLContext:
    cafile = os.environ.get("SSL_CERT_FILE")
    return ssl.create_default_context(cafile=cafile if cafile and os.path.exists(cafile) else None)


def latest_version(release: Release) -> str:
    with urllib.request.urlopen(release.version_url, timeout=30, context=_context()) as r:
        version = r.read(64).decode("ascii", "replace").strip()
    if not version or not all(c.isdigit() or c == "." for c in version):
        raise UpstreamAppError(f"the developer's site answered {version[:20]!r} for a version")
    return version


def _download(url: str, dest: Path, share: tuple[int, int] | None = None) -> None:
    """To a file, reporting percent within `share` of the whole job."""
    with urllib.request.urlopen(url, timeout=60, context=_context()) as r, dest.open("wb") as out:
        total = int(r.headers.get("Content-Length") or 0)
        done = last = 0
        while chunk := r.read(1 << 16):
            out.write(chunk)
            done += len(chunk)
            if share and total:
                lo, hi = share
                percent = lo + (hi - lo) * done // total
                if percent != last:
                    _say(percent=percent, status="Downloading…")
                    last = percent


def verify(release: Release, tarball: Path, signature: Path, keys: Path = KEYS) -> None:
    """gpgv against the developer's keyring, and the signature must be by
    the pinned key, not merely by some key in that file."""
    res = subprocess.run(
        ["gpgv", "--status-fd", "1", "--keyring", str(keys / release.keyring),
         str(signature), str(tarball)],
        capture_output=True, text=True)
    valid = [line.split()[2] for line in res.stdout.splitlines()
             if line.startswith("[GNUPG:] VALIDSIG ") and len(line.split()) > 2]
    if res.returncode != 0 or release.fingerprint not in valid:
        raise UpstreamAppError("the download's signature did not check out, so it was not installed")


def _unpack(tarball: Path, dest: Path) -> None:
    with tarfile.open(tarball, "r:xz") as tar:
        tar.extractall(dest, filter="data")


def _desktop_entry(text: str, app: Path, icon: Path, ref: str) -> str:
    """kitty's own desktop file, pointed at this copy: absolute Exec and
    TryExec (nothing a desktop launches has ~/.local/bin on its PATH) and
    an absolute icon."""
    out = []
    for line in text.splitlines():
        key = line.split("=", 1)[0]
        if key in ("Exec", "TryExec") and "=" in line:
            command = line.split("=", 1)[1]
            first, _, rest = command.partition(" ")
            if "/" not in first and (app / "bin" / first).exists():
                command = f"{app / 'bin' / first}{' ' + rest if rest else ''}"
            line = f"{key}={command}"
        elif key == "Icon":
            line = f"Icon={icon}"
        out.append(line)
        if line.strip() == "[Desktop Entry]":
            out.append(f"{MARK}={ref}")
    return "\n".join(out) + "\n"


def _ours(path: Path, app: Path) -> bool:
    try:
        if path.is_symlink():
            return os.readlink(path).startswith(str(app) + "/")
        return MARK + "=" in path.read_text()
    except OSError:
        return False


def link(home: Path, name: str) -> None:
    """Launchers and commands for the installed copy, into the account's
    own ~/.local. Anything there that is not ours is left alone."""
    release = RELEASES[name]
    app = home / release.directory
    apps_dir = home / ".local/share/applications"
    apps_dir.mkdir(parents=True, exist_ok=True)
    for file in release.desktop_files:
        source = app / "share/applications" / file
        if not source.exists():
            continue
        target = apps_dir / file
        if target.exists() and not _ours(target, app):
            continue
        target.write_text(_desktop_entry(source.read_text(), app, app / release.icon, PREFIX + name))
    bin_dir = home / ".local/bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    for binary in release.binaries:
        target = bin_dir / binary
        if target.is_symlink() and _ours(target, app):
            target.unlink()
        if not target.exists() and not target.is_symlink():
            os.symlink(app / "bin" / binary, target)


def unlink(home: Path, name: str) -> None:
    release = RELEASES[name]
    app = home / release.directory
    for file in release.desktop_files:
        target = home / ".local/share/applications" / file
        if target.exists() and _ours(target, app):
            target.unlink()
    for binary in release.binaries:
        target = home / ".local/bin" / binary
        if _ours(target, app):
            target.unlink()


def install_into(home: Path, name: str, version: str, fetch=None, keys: Path = KEYS) -> None:
    """Download, check, unpack beside the old copy, then swap. Until the
    swap nothing the account has is touched; if the swap fails the old
    copy is put back."""
    release = RELEASES[name]
    arch = release.arches.get(platform.machine())
    if arch is None:
        raise UpstreamAppError(f"{name} has no build for this kind of computer")
    url = release.tarball.format(version=version, arch=arch)
    fetch = fetch or _download
    app = home / release.directory
    app.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f".{name}-", dir=app.parent) as tmp:
        work = Path(tmp)
        tarball, signature = work / "app.txz", work / "app.txz.sig"
        fetch(url, tarball, (2, 90))
        fetch(url + ".sig", signature, None)
        _say(percent=92, status="Checking…")
        verify(release, tarball, signature, keys)
        _say(percent=95, status="Installing…")
        fresh = work / "app"
        _unpack(tarball, fresh)
        (fresh / VERSION_FILE).write_text(version + "\n")
        old = work / "old"
        if app.exists():
            app.rename(old)
        try:
            fresh.rename(app)
        except OSError:
            if old.exists():
                old.rename(app)
            raise
    link(home, name)
    _say(percent=100, status="Done")


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 2 or args[0] not in ("install", "upgrade", "remove") or args[1] not in RELEASES:
        print("usage: kosher-upstream-app install|upgrade|remove " + "|".join(RELEASES),
              file=sys.stderr)
        return 2
    action, name = args
    home = Path(os.environ.get("HOME") or os.path.expanduser("~"))
    try:
        if action == "remove":
            unlink(home, name)
            shutil.rmtree(home / RELEASES[name].directory, ignore_errors=True)
            return 0
        version = latest_version(RELEASES[name])
        if action == "upgrade" and installed_version(home, name) == version:
            link(home, name)
            _say(percent=100, status="Up to date")
            return 0
        install_into(home, name, version)
        return 0
    except (UpstreamAppError, OSError, tarfile.TarError) as e:
        reason = str(e)
        if isinstance(e, OSError) and not isinstance(e, UpstreamAppError):
            reason = f"could not reach the developer's site: {e}" \
                if isinstance(e, urllib.error.URLError) else str(e)
        _say(error=reason)
        return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
