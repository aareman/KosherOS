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

The SSH agent needs nothing from here: the app from nixpkgs runs it, and a
person points ssh at ~/.1password/agent.sock (docs/apps.md). Browser
integration (1Password-BrowserSupport, the `onepassword` group) is not done
here yet.
"""

from __future__ import annotations

import grp
import logging
import os
import pwd
import subprocess
import tempfile
from pathlib import Path

from . import nixapps

log = logging.getLogger(__name__)

WRAPPERS = Path("/run/wrappers/bin")
STORE = "/nix/store/"
# The packages that bring 1Password to an account; either one means the
# account wants `op` to reach the app.
REFS = frozenset({"nixpkgs#_1password-gui", "nixpkgs#_1password-cli"})
CLI_ATTR = "_1password-cli"
OP = "op"
OP_GROUP = "onepassword-cli"
BUILD_TIMEOUT = 900


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


def build_cli(run=subprocess.run) -> Path:
    """Build (fetch) nixpkgs' 1Password CLI as root; its store path."""
    argv = ["env", "NIXPKGS_ALLOW_UNFREE=1", f"NIX_SSL_CERT_FILE={nixapps.CA_BUNDLE}",
            "nix", "build", "--impure", "--no-link", "--print-out-paths",
            f"{nixapps.PREFIX}{CLI_ATTR}"]
    try:
        res = run(argv, capture_output=True, text=True, timeout=BUILD_TIMEOUT)
    except (OSError, subprocess.SubprocessError) as e:
        raise WrapperError(f"could not build the 1Password CLI: {e}") from None
    paths = [line.strip() for line in (res.stdout or "").splitlines() if line.strip()]
    if res.returncode != 0 or not paths:
        raise WrapperError("could not build the 1Password CLI: "
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


def refresh(installed_by_uid: dict[int, set[str]] | None = None, *,
            build=build_cli, directory: Path = WRAPPERS) -> str:
    """Put /run/wrappers/bin/op in step with who has 1Password. Returns
    what it did: "installed", "removed" or "unchanged"."""
    if installed_by_uid is None:
        installed_by_uid = accounts_installed()
    if not wanted(installed_by_uid):
        return "removed" if drop(OP, directory) else "unchanged"
    store_path = build()
    install(store_path / "bin" / OP, OP, OP_GROUP, directory)
    log.info("1password: %s is %s's op, setgid %s", directory / OP, store_path, OP_GROUP)
    return "installed"
