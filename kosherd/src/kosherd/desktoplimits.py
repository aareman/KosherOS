"""Per-account GNOME desktop limits, separate from the network boundary.

Root owns each dconf database and its locks. The user's session selects
that profile at login. This closes GNOME's command prompt and user-switch
controls; it does not prevent execution of arbitrary local programs.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

from . import appaccess

PROFILE_DIR = Path("/etc/dconf/profile")
DATABASE_DIR = Path("/etc/dconf/db")
KEYS = ("disable-command-line", "disable-user-switching")
TERMINALS = ("/usr/bin/ptyxis", "/usr/bin/kgx", "/usr/bin/gnome-terminal",
             "/usr/bin/gnome-terminal-server", "/usr/bin/xterm")


def restricted(user) -> bool:
    if user.admin or user.developer_tools:
        return False
    return bool(appaccess.access_of(user) == "approved" or user.apps
                or user.blocked_apps or user.blocked_app_kinds)


def profile_name(uid: int) -> str:
    if type(uid) is not int or uid < 1000:
        raise ValueError("desktop profiles belong to human accounts")
    return f"kosher-{uid}"


def selected_profile(uid: int, directory: Path = PROFILE_DIR) -> Path | None:
    if uid < 1000:
        return None  # never replace the greeter's own dconf profile
    path = directory / profile_name(uid)
    return path if path.is_file() else None


def _write(path: Path, text: str) -> bool:
    try:
        if path.read_text() == text:
            return False
    except FileNotFoundError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as fh:
        temp = Path(fh.name)
        try:
            fh.write(text)
            fh.flush()
            os.fchmod(fh.fileno(), 0o644)
            temp.replace(path)
        finally:
            temp.unlink(missing_ok=True)
    return True


def apply(policy, *, profile_dir: Path = PROFILE_DIR,
          database_dir: Path = DATABASE_DIR, run=None) -> None:
    """Compile changed root-owned databases, keeping profiles stable per uid.

    Profile selection takes effect at the next sign-in. Subsequent policy
    changes update the same database and its locks through dconf update.
    """
    changed = False
    profiles = []
    for user in policy.effective_users():
        name = profile_name(user.uid)
        locked = restricted(user)
        value = "true" if locked else "false"
        source = database_dir / (name + ".d")
        changed |= _write(source / "00-policy", "[org/gnome/desktop/lockdown]\n" +
                          "".join(f"{key}={value}\n" for key in KEYS))
        changed |= _write(source / "locks" / "00-policy", "".join(
            f"/org/gnome/desktop/lockdown/{key}\n" for key in KEYS) if locked else "")
        changed |= not (database_dir / name).is_file()
        profiles.append((profile_dir / name,
                         f"user-db:user\nsystem-db:{name}\nsystem-db:local\n"))
    if changed:
        (run or subprocess.run)(["dconf", "update", str(database_dir)],
                                check=True, capture_output=True, text=True, timeout=30)
    # Publish profiles only after successful compilation.
    for path, text in profiles:
        _write(path, text)
