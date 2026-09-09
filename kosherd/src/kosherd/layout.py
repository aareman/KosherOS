"""Apply an account's desktop layout when they sign in.

The layout is a per-account setting kosherd holds (see policy.LAYOUTS). It
is a preference, not a protection — the filter is per-uid at the network
layer and does not care which shell draws the windows — so it is applied
the simple way: a systemd user unit runs this as the person signing in,
asks kosherd which layout is theirs, and writes the GNOME settings that
layout needs into their own settings database.

Why not lock those settings machine-wide? dconf locks are global: one value
for every account. The layout differs per account, and a user's own
database is exactly the per-account layer dconf has. The cost is that a
person can change these keys mid-session (turn the taskbar off, say); the
price of that is a desktop they have to live with until they sign in again,
when this puts it back. The keys the image does lock are the ones that are
the same for everyone (see /etc/dconf/db/local.d/locks).

The advanced layout is a separate niri session rather than GNOME, chosen
for the account at the login screen by kosherd. Under niri this only seeds
Noctalia's configuration once, so the shell starts with the polkit agent
on and the KosherOS wallpaper, and leaves the person's own edits alone
afterwards. Should someone with the advanced layout sign in to GNOME
anyway (the login screen still offers it), they get the classic layout —
a sane desktop rather than a bare one.
"""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from .policy import DEFAULT_LAYOUT, LAYOUTS

log = logging.getLogger("kosher-layout")

# The GNOME Shell extensions the image ships, by uuid. All three live under
# /usr/share/gnome-shell/extensions, so no account can remove them and every
# account can be given them.
EXT_DASH_TO_PANEL = "dash-to-panel@jderose9.github.com"
# The start menu: a button that says "Apps", because the picture alone did
# not tell anyone where the apps were.
EXT_ARCMENU = "arcmenu@arcmenu.com"
EXT_APPINDICATOR = "appindicatorsupport@rgcjonas.gmail.com"
EXT_PAPERWM = "paperwm@paperwm.github.com"

# Where the seed for a Noctalia configuration ships, and where it goes.
NOCTALIA_SEED = Path("/usr/share/kosher/desktop/noctalia.toml")
NOCTALIA_CONFIG = Path(".config/noctalia/config.toml")


def _gvariant_list(items: list[str]) -> str:
    return "[" + ", ".join(f"'{i}'" for i in items) + "]"


# (schema, key, value as GVariant text) — what each layout sets. The lists
# are complete, not diffs: whatever the previous layout wrote is overridden.
PLANS: dict[str, list[tuple[str, str, str]]] = {
    # A taskbar along the bottom with a start button, minimise buttons on
    # windows, one workspace so nothing can get lost on another, no hot
    # corner to trigger by accident. What a Windows, Mac or ChromeOS user
    # already knows.
    "classic": [
        ("org.gnome.shell", "enabled-extensions",
         _gvariant_list([EXT_DASH_TO_PANEL, EXT_ARCMENU, EXT_APPINDICATOR])),
        ("org.gnome.desktop.wm.preferences", "button-layout",
         "'appmenu:minimize,maximize,close'"),
        ("org.gnome.desktop.interface", "enable-hot-corners", "false"),
        ("org.gnome.mutter", "dynamic-workspaces", "false"),
        ("org.gnome.desktop.wm.preferences", "num-workspaces", "1"),
        ("org.gnome.mutter", "edge-tiling", "true"),
    ],
    # GNOME as GNOME ships it, plus PaperWM's scrolling tiling. PaperWM
    # manages workspaces and window placement itself, so GNOME's dynamic
    # workspaces come back and its edge tiling gets out of the way.
    "tiling": [
        ("org.gnome.shell", "enabled-extensions",
         _gvariant_list([EXT_PAPERWM, EXT_APPINDICATOR])),
        ("org.gnome.desktop.wm.preferences", "button-layout", "'appmenu:close'"),
        ("org.gnome.desktop.interface", "enable-hot-corners", "false"),
        ("org.gnome.mutter", "dynamic-workspaces", "true"),
        # GNOME's own default; only read when workspaces are not dynamic,
        # but written so that classic's single workspace is undone.
        ("org.gnome.desktop.wm.preferences", "num-workspaces", "4"),
        ("org.gnome.mutter", "edge-tiling", "false"),
    ],
}
# Not a GNOME layout; when GNOME is what got started anyway, be classic.
PLANS["advanced"] = PLANS["classic"]

assert set(PLANS) == set(LAYOUTS)


def plan(layout: str) -> list[tuple[str, str, str]]:
    """The settings to write for a layout; the default's for an unknown one."""
    return list(PLANS.get(layout, PLANS[DEFAULT_LAYOUT]))


def current_desktop() -> str:
    return os.environ.get("XDG_CURRENT_DESKTOP", "").lower()


def under_niri(desktop: str | None = None) -> bool:
    return "niri" in (desktop if desktop is not None else current_desktop())


def apply_gsettings(steps, run=subprocess.run) -> list[str]:
    """Write each setting; returns what failed, one line each, and goes on
    past failures — one missing schema must not cost the rest of the layout."""
    failures = []
    for schema, key, value in steps:
        res = run(["gsettings", "set", schema, key, value],
                  capture_output=True, text=True)
        if res.returncode != 0:
            failures.append(f"{schema} {key}: {res.stderr.strip() or res.returncode}")
    return failures


def seed_noctalia(home: Path, seed: Path = NOCTALIA_SEED) -> bool:
    """Give a first niri sign-in a Noctalia configuration; never overwrite.

    Noctalia reads only the person's own ~/.config; there is no system-wide
    layer to put defaults in. What matters in the seed is the polkit agent
    (without one the admin app's prompts have nowhere to appear) and the
    KosherOS wallpaper. Everything else is Noctalia's own default.
    """
    target = home / NOCTALIA_CONFIG
    if target.exists() or not seed.exists():
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(seed, target)
    return True


def fetch_layout(attempts: int = 5, delay: float = 1.0) -> str:
    """Ask kosherd; fall back to the default rather than to no desktop."""
    from .client import DaemonClient

    last = None
    for _ in range(attempts):
        try:
            layout = DaemonClient().my_layout()
            if layout in LAYOUTS:
                return layout
            log.warning("kosherd answered an unknown layout %r", layout)
            return DEFAULT_LAYOUT
        except Exception as e:  # noqa: BLE001 - the bus may not be up yet
            last = e
            time.sleep(delay)
    log.warning("could not reach kosherd (%s); using the %s layout", last, DEFAULT_LAYOUT)
    return DEFAULT_LAYOUT


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="apply this account's KosherOS desktop layout")
    p.add_argument("--layout", choices=LAYOUTS,
                   help="apply this layout instead of asking kosherd")
    p.add_argument("--dry-run", action="store_true",
                   help="print what would be written and change nothing")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(name)s: %(message)s",
                        stream=sys.stderr)

    layout = args.layout or fetch_layout()
    if under_niri():
        # Not a GNOME session: nothing to write into GNOME Shell. Seed the
        # shell's own configuration on the first sign-in and leave it.
        if args.dry_run:
            print("under niri: would seed Noctalia's configuration if absent")
            return 0
        if seed_noctalia(Path.home()):
            log.info("seeded Noctalia's configuration")
        return 0

    steps = plan(layout)
    if args.dry_run:
        for schema, key, value in steps:
            print(f"gsettings set {schema} {key} {value}")
        return 0
    failures = apply_gsettings(steps)
    for line in failures:
        log.warning("could not set %s", line)
    log.info("applied the %s layout (%d settings, %d failed)",
             layout, len(steps), len(failures))
    return 0


if __name__ == "__main__":
    sys.exit(main())
