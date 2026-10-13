"""Each card says how the app is installed: a Flatpak from Flathub, or a
Nix package into the account's own profile. Two different things, and a
person choosing between the two VS Codes needs to see which is which."""

import pytest

gi = pytest.importorskip("gi")
try:
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Gtk
except ValueError:  # pragma: no cover - no typelibs here
    pytest.skip("GTK4/libadwaita not available", allow_module_level=True)
if not Gtk.init_check():  # pragma: no cover - no display
    pytest.skip("no display", allow_module_level=True)

from kosherstore.app import AppCard, source_of  # noqa: E402
from test_widgets import window  # noqa: E402,F401  (the fixture: a Store on the demo daemon)


def test_the_source_is_read_from_the_ref():
    assert source_of({"ref": "com.visualstudio.code"}) == "Flatpak"
    assert source_of({"ref": "nixpkgs#claude-code"}) == "Nix"


def test_an_entry_may_say_its_source_outright():
    assert source_of({"ref": "x", "source": "image"}) == "Built in"
    assert source_of({"ref": "x", "source": "raw"}) == "Raw"


def test_the_card_shows_the_source_beside_the_name(window):
    card = AppCard({"ref": "nixpkgs#claude-code", "name": "Claude Code",
                    "summary": "Anthropic's coding agent, in the terminal"}, window)
    labels = []
    heading = card.get_first_child().get_first_child().get_next_sibling().get_first_child()
    child = heading.get_first_child()
    while child is not None:
        labels.append(child.get_label())
        child = child.get_next_sibling()
    assert labels == ["Claude Code", "Nix"]
