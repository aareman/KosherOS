"""Keep a Nix program's GTK out of the KosherOS apps.

A GTK app from nixpkgs (Ghostty, kitty and the like) starts through a
wrapper that points GI_TYPELIB_PATH, GIO_EXTRA_MODULES and their kin at its
own copies of GTK in /nix/store, and every shell opened in that terminal
inherits them. The KosherOS apps run on Fedora's Python and GTK, so started
from such a shell they loaded Nix's GTK into a Fedora process, and that GTK
needs a newer C library than Fedora's: "version `GLIBC_2.44' not found".
Started from the app launcher or Ptyxis they were fine.

Each app calls drop_nix() in its package's __init__, before anything
imports gi. Only /nix/store entries go; a person's own ~/.nix-profile on
XDG_DATA_DIRS stays, as do the variables when nothing in them is Nix's.
Programs the apps start inherit the cleaned environment. A Python that
is itself from Nix (the developer shell, which runs the test suites) needs
Nix's GTK and keeps it.
"""

from __future__ import annotations

import os
import sys

NIX_STORE = "/nix/store/"

# Colon-separated lists of directories.
PATH_LISTS = (
    "GI_TYPELIB_PATH",
    "GIO_EXTRA_MODULES",
    "GTK_PATH",
    "GST_PLUGIN_PATH",
    "GST_PLUGIN_SYSTEM_PATH",
    "GST_PLUGIN_PATH_1_0",
    "GST_PLUGIN_SYSTEM_PATH_1_0",
    "XDG_DATA_DIRS",
)
# A single file or directory.
SINGLE = (
    "GDK_PIXBUF_MODULE_FILE",
    "GDK_PIXBUF_MODULEDIR",
    "GSETTINGS_SCHEMA_DIR",
    "GTK_EXE_PREFIX",
    "GTK_DATA_PREFIX",
)


def drop_nix(environ=None, prefix: str | None = None) -> list[str]:
    """Remove /nix/store entries from the GTK variables; return the names
    that changed. Nothing changes under a Python from /nix/store."""
    if (sys.prefix if prefix is None else prefix).startswith(NIX_STORE):
        return []
    env = os.environ if environ is None else environ
    changed = []
    for name in PATH_LISTS:
        value = env.get(name)
        if value is None:
            continue
        kept = [p for p in value.split(":") if p and not p.startswith(NIX_STORE)]
        if kept == [p for p in value.split(":") if p]:
            continue
        if kept:
            env[name] = ":".join(kept)
        else:
            del env[name]
        changed.append(name)
    for name in SINGLE:
        if env.get(name, "").startswith(NIX_STORE):
            del env[name]
            changed.append(name)
    return changed
