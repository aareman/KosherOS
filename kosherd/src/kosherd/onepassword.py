"""1Password's `op` CLI integration, for 1Password from nixpkgs (#234).

1Password's Flatpak cannot run its SSH agent (it turns the agent off in a
sandbox) or let `op` use the desktop app: the app trusts a CLI process only
when its group is `onepassword-cli`, which 1Password's own packages get by
making `op` setgid to that group. A Flatpak cannot create the group or set
the bit, and neither can the Nix store, which holds no setgid files.

So 1Password comes from nixpkgs (`_1password-gui`, `_1password-cli`, in an
account's own profile like any other Nix app), and kosherd does what the
NixOS module does with security.wrappers: it keeps a copy of `op`,
root:onepassword-cli and setgid, at /run/wrappers/bin/op, and login shells
put /run/wrappers/bin first on PATH (profile.d/zz-kosher-wrappers.sh). The
group's gid is fixed in sysusers.d so it is the same on every machine.

The copy comes from a store path kosherd builds itself, as root, from
nixpkgs: never from a path an account names. An account's profile manifest
is a file in its home, so it only says whether anybody wants 1Password at
all. The group grants nothing else on the machine; what it buys is the
desktop app's trust, and each account's app listens only in that account's
own runtime directory.

Browser integration is the same again: the extension in Firefox talks to
the app through 1Password-BrowserSupport, started from the native
messaging manifest the app writes into ~/.mozilla (pointing, for a nixpkgs
build, at /run/wrappers/bin/1Password-BrowserSupport), and the app trusts
it only running as group `onepassword`. The image also ships that
manifest system-wide, for before the app's first start.

Each build the copies come from has a GC root, because the copies are
linked against libraries in the store.

The SSH agent needs nothing from here: the app from nixpkgs runs it, and a
person points ssh at ~/.1password/agent.sock (docs/supported.md).
"""

from __future__ import annotations

import grp
import logging
import os
import pwd
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from . import nixapps

log = logging.getLogger(__name__)

WRAPPERS = Path("/run/wrappers/bin")
STORE = "/nix/store/"
# The packages that bring 1Password to an account; either one means the
# account wants `op` to reach the app.
REFS = frozenset({"nixpkgs#_1password-gui", "nixpkgs#_1password-cli"})
BUILD_TIMEOUT = 900
# Roots that keep the builds the copies came from (nix-collect-garbage
# leaves anything reachable from here).
GCROOTS = Path("/nix/var/nix/gcroots")


@dataclass(frozen=True)
class Wrapper:
    name: str    # in /run/wrappers/bin
    attr: str    # the nixpkgs attribute kosherd builds it from
    file: str    # where it is in that build
    group: str   # what it is setgid to: the group the app trusts
    refs: frozenset[str]  # any of these installed means it is wanted

    def wanted(self, installed_by_uid: dict[int, set[str]]) -> bool:
        return any(refs & self.refs for refs in installed_by_uid.values())


WRAPPED = (
    # The CLI integration: the app trusts an op running as onepassword-cli.
    Wrapper("op", "_1password-cli", "bin/op", "onepassword-cli", REFS),
    # The browser integration: Firefox starts this from the native
    # messaging manifest the app writes (path /run/wrappers/bin/... for a
    # nixpkgs build), and the app trusts it running as onepassword.
    Wrapper("1Password-BrowserSupport", "_1password-gui",
            "share/1password/1Password-BrowserSupport", "onepassword",
            frozenset({"nixpkgs#_1password-gui"})),
)


class WrapperError(Exception):
    pass


def wanted(installed_by_uid: dict[int, set[str]]) -> bool:
    """Does any account have 1Password from nixpkgs?"""
    return any(refs & REFS for refs in installed_by_uid.values())


def accounts_installed() -> dict[int, set[str]]:
    """Each person's Nix apps, read from their profile manifests."""
    found = {}
    for p in pwd.getpwall():
        if 1000 <= p.pw_uid < 60000:
            try:
                found[p.pw_uid] = nixapps.installed(p.pw_uid)
            except Exception:  # noqa: BLE001 - one unreadable home is not all of them
                continue
    return found


def build(attr: str, run=subprocess.run, gcroots: Path = GCROOTS) -> Path:
    """Build (fetch) a nixpkgs attribute as root; its store path. The out
    link is a GC root: the copies in /run/wrappers are linked against
    libraries in the store, and a garbage collection would otherwise take
    those away from under them."""
    link = gcroots / f"kosher-{attr}"
    argv = ["env", "NIXPKGS_ALLOW_UNFREE=1", f"NIX_SSL_CERT_FILE={nixapps.CA_BUNDLE}",
            "nix", "build", "--impure", "--out-link", str(link), "--print-out-paths",
            f"{nixapps.PREFIX}{attr}"]
    try:
        res = run(argv, capture_output=True, text=True, timeout=BUILD_TIMEOUT)
    except (OSError, subprocess.SubprocessError) as e:
        raise WrapperError(f"could not build {attr}: {e}") from None
    paths = [line.strip() for line in (res.stdout or "").splitlines() if line.strip()]
    if res.returncode != 0 or not paths:
        raise WrapperError(f"could not build {attr}: "
                           + ((res.stderr or "").strip().splitlines() or ["no output"])[-1])
    return Path(paths[-1])


def source_ok(path: Path) -> bool:
    """A file kosherd may make setgid: a regular file in the Nix store,
    owned by root, reached without leaving the store."""
    try:
        real = Path(os.path.realpath(path))
        st = real.lstat()
    except OSError:
        return False
    import stat

    return (str(real).startswith(STORE) and stat.S_ISREG(st.st_mode)
            and st.st_uid == 0 and not st.st_mode & 0o022)


def install(source: Path, name: str, group: str, directory: Path = WRAPPERS) -> Path:
    """Put a root:<group> 2755 copy of `source` at directory/name, replacing
    any copy there in one rename."""
    if not source_ok(source):
        raise WrapperError(f"{source} is not a file kosherd will make setgid")
    gid = grp.getgrnam(group).gr_gid
    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(directory, 0o755)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=f".{name}.")
    try:
        with os.fdopen(fd, "wb") as out, open(os.path.realpath(source), "rb") as src:
            while chunk := src.read(1 << 20):
                out.write(chunk)
        os.chown(tmp, 0, gid)
        # chown clears the setgid bit, so the mode comes after it.
        os.chmod(tmp, 0o2755)
        os.replace(tmp, directory / name)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return directory / name


def drop(name: str, directory: Path = WRAPPERS) -> bool:
    path = directory / name
    if path.exists() or path.is_symlink():
        path.unlink()
        return True
    return False


def in_step(installed_by_uid: dict[int, set[str]], directory: Path = WRAPPERS) -> bool:
    """Is every wanted copy there, and no unwanted one? The periodic check
    builds nothing when so."""
    return all((directory / w.name).exists() == w.wanted(installed_by_uid) for w in WRAPPED)


def refresh(installed_by_uid: dict[int, set[str]] | None = None, *,
            build=build, directory: Path = WRAPPERS,
            gcroots: Path = GCROOTS) -> dict[str, str]:
    """Put the copies in /run/wrappers/bin in step with who has 1Password.
    Returns, per copy, "installed", "removed" or "unchanged"."""
    if installed_by_uid is None:
        installed_by_uid = accounts_installed()
    done = {}
    for w in WRAPPED:
        if not w.wanted(installed_by_uid):
            (gcroots / f"kosher-{w.attr}").unlink(missing_ok=True)
            done[w.name] = "removed" if drop(w.name, directory) else "unchanged"
            continue
        store_path = build(w.attr)
        install(store_path / w.file, w.name, w.group, directory)
        log.info("1password: %s is %s's, setgid %s", directory / w.name, store_path, w.group)
        done[w.name] = "installed"
    return done
