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

import json
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


CATALOG = Path("/etc/kosher/catalog.json")
MARK = "X-KosherOS-Nix"


def launcher(entry: dict, command: Path) -> str:
    """A desktop entry for a terminal tool the Store installed: the app
    grid shows "Claude Code", and opening it is a terminal running it.
    The command's absolute path through the profile link, since nothing
    a desktop launches has the profile on its PATH."""
    name = entry.get("name") or entry["ref"]
    return (
        "[Desktop Entry]\n"
        "Type=Application\n"
        f"Name={name}\n"
        f"Comment={entry.get('summary', '')}\n"
        f"Exec={command}\n"
        "Terminal=true\n"
        "Icon=utilities-terminal\n"
        "Categories=Development;ConsoleOnly;\n"
        f"{MARK}={entry['ref']}\n"
    )


def sync_launchers(profile: Path | None, dest: Path, catalog: Path | None = None) -> tuple[int, int]:
    """Terminal tools from the catalog (entries with an `exec`): a launcher
    for each one whose command is in the profile, none for the rest.
    Returns (written, removed)."""
    try:
        entries = json.loads((catalog or CATALOG).read_text()).get("apps", [])
    except (OSError, ValueError):
        entries = []
    wanted: dict[str, str] = {}
    for entry in entries:
        ref, cmd = entry.get("ref", ""), entry.get("exec")
        if not cmd or not ref.startswith("nixpkgs#") or profile is None:
            continue
        command = profile / "bin" / cmd
        if command.exists():
            wanted[f"kosheros-{ref.replace('#', '-')}.desktop"] = launcher(entry, command)
    written = removed = 0
    if dest.is_dir():
        for file in dest.glob("kosheros-nixpkgs-*.desktop"):
            if file.name not in wanted:
                try:
                    if MARK in file.read_text():
                        file.unlink()
                        removed += 1
                except OSError:
                    pass
    for name, text in wanted.items():
        file = dest / name
        try:
            if file.read_text() == text:
                continue
        except OSError:
            pass
        dest.mkdir(parents=True, exist_ok=True)
        file.write_text(text)
        written += 1
    return written, removed


def profile_link(home: Path) -> Path | None:
    share = profile_share(home)
    return share.parent if share else None


def sync(home: Path) -> tuple[int, int]:
    share = profile_share(home)
    data = Path(os.environ.get("XDG_DATA_HOME") or home / ".local/share")
    made, removed = sync_tree(share / "applications" if share else None,
                              data / "applications", (".desktop",))
    m2, r2 = sync_tree(share / "icons" if share else None, data / "icons",
                       (".png", ".svg", ".xpm"))
    m3, r3 = sync_launchers(profile_link(home), data / "applications")
    return made + m2 + m3, removed + r2 + r3


def main(argv: list[str] | None = None) -> int:
    home = Path(os.environ.get("HOME") or os.path.expanduser("~"))
    made, removed = sync(home)
    if "-q" not in (argv if argv is not None else sys.argv[1:]):
        print(f"desktop entries: {made} linked, {removed} stale removed")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
