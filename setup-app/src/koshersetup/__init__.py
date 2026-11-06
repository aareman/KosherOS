"""KosherOS first-boot setup wizard."""

__version__ = "0.1.0"

# Before anything imports gi: a shell inside a Nix terminal points GTK's
# search paths at Nix's own GTK (kosherd/gtkenv.py).
from kosherd import gtkenv as _gtkenv  # noqa: E402

_gtkenv.drop_nix()
