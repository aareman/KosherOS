"""An app from nixpkgs reaches the app grid: kosher-nix-desktop.

A Nix profile keeps its .desktop files and icons under its own share
directory, which GNOME does not read. Putting that directory on the
session's XDG_DATA_DIRS from /etc/environment.d looked like the answer
and took the login screen down: GDM's greeter needs its own schema
directory first on that variable, and any value set there replaced the
greeter's (and would have dropped the Flatpak export directories for
everyone else). "No GSettings schemas are installed on the system",
six times, then a black screen.

So instead the account's own ~/.local/share — which GNOME always reads,
first — gets a symlink to each desktop file and icon in the profile.
This runs AS THE ACCOUNT (kosherd calls it through runuser after an
install, an upgrade or a removal), so the links are the account's own
files, and it is idempotent: a link that points into the store and no
longer resolves is removed, a file in the profile that has no link yet
gets one, and anything the person put there themselves is left alone.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

STORE = "/nix/store"


def profile_share(home: Path) -> Path | None:
    """The profile's share directory, whichever layout nix used."""
    state = Path(os.environ.get("XDG_STATE_HOME") or home / ".local/state")
    for candidate in (state / "nix/profile", home / ".nix-profile",
                      state / "nix/profiles/profile"):
        share = candidate / "share"
        if share.is_dir():
            return share
    return None


def _ours(link: Path) -> bool:
    """A symlink this helper made: it points into the Nix store."""
    try:
        return link.is_symlink() and os.readlink(link).startswith(STORE + "/")
    except OSError:
        return False


def _target(path: Path) -> str:
    """Where a profile path really is in the store, so the link survives
    the profile moving on to a new generation that no longer has it."""
    return os.path.realpath(path)


def sync_tree(source: Path | None, dest: Path, suffixes: tuple[str, ...]) -> tuple[int, int]:
    """Mirror `source` into `dest` as symlinks: (made, removed)."""
    made = removed = 0
    # Stale links first: a generation that dropped the app.
    if dest.is_dir():
        for link in dest.rglob("*"):
            if _ours(link) and not link.exists():
                link.unlink()
                removed += 1
    if source is None or not source.is_dir():
        return made, removed
    for file in source.rglob("*"):
        if not file.is_file() or file.suffix not in suffixes:
            continue
        link = dest / file.relative_to(source)
        if link.exists() or link.is_symlink():
            continue  # theirs, or already ours
        link.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(_target(file), link)
        made += 1
    return made, removed


def sync(home: Path) -> tuple[int, int]:
    share = profile_share(home)
    data = Path(os.environ.get("XDG_DATA_HOME") or home / ".local/share")
    made, removed = sync_tree(share / "applications" if share else None,
                              data / "applications", (".desktop",))
    m2, r2 = sync_tree(share / "icons" if share else None, data / "icons",
                       (".png", ".svg", ".xpm"))
    return made + m2, removed + r2


def main(argv: list[str] | None = None) -> int:
    home = Path(os.environ.get("HOME") or os.path.expanduser("~"))
    made, removed = sync(home)
    if "-q" not in (argv if argv is not None else sys.argv[1:]):
        print(f"desktop entries: {made} linked, {removed} stale removed")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
