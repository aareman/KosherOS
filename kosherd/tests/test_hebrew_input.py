"""Hebrew out of the box: English first, Hebrew one switch away, fonts in.

A frum family types Hebrew. The machine should come with both layouts set
up everywhere a keyboard is used — the desktop, the login screen, the
advanced (niri) session — with the switcher visible, and with proper
Hebrew fonts installed. English stays the default so a Windows or Mac user
is not surprised on the first keystroke.
"""

import configparser
import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).parents[2]
FILES = ROOT / "os-image/files"
CONTAINERFILE = (ROOT / "os-image/Containerfile").read_text()
DESKTOP = FILES / "etc/dconf/db/local.d/30-kosheros-input"
GREETER = FILES / "etc/dconf/db/gdm.d/20-kosheros-input"
KEYMAP = FILES / "etc/X11/xorg.conf.d/00-keyboard.conf"
NOCTALIA = FILES / "usr/share/kosher/desktop/noctalia.toml"


def _sources(path: Path) -> list[tuple[str, str]]:
    cp = configparser.ConfigParser()
    cp.read_string(path.read_text())
    value = cp["org/gnome/desktop/input-sources"]["sources"]
    return re.findall(r"\('(\w+)', '(\w+)'\)", value)


def test_the_desktop_has_english_first_and_hebrew_second():
    assert _sources(DESKTOP) == [("xkb", "us"), ("xkb", "il")]


def test_the_login_screen_has_the_same_two_layouts():
    # GDM runs on its own dconf profile; without this a Hebrew password
    # could not be typed at the login screen.
    assert _sources(GREETER) == _sources(DESKTOP)


def test_alt_shift_switches_like_windows_and_super_space_stays():
    for path in (DESKTOP, GREETER):
        cp = configparser.ConfigParser()
        cp.read_string(path.read_text())
        assert "grp:alt_shift_toggle" in cp["org/gnome/desktop/input-sources"]["xkb-options"]


def test_the_advanced_session_reads_the_same_keymap_from_the_system():
    # niri takes its layout from systemd-localed, which serves this file.
    text = KEYMAP.read_text()
    assert re.search(r'Option\s+"XkbLayout"\s+"us,il"', text)
    assert re.search(r'Option\s+"XkbOptions"\s+"grp:alt_shift_toggle"', text)


def test_the_noctalia_bar_shows_the_layout_switcher():
    doc = tomllib.loads(NOCTALIA.read_text())
    assert "keyboard_layout" in doc["bar"]["main"]["end"]


def test_the_input_sources_are_not_locked():
    # A default, not a lock: a family that also types Yiddish or Russian
    # adds a source in Settings and it must stay.
    for lock in (FILES / "etc/dconf/db/local.d/locks").glob("*"):
        assert "input-sources" not in lock.read_text()


def test_hebrew_fonts_ship_in_the_image():
    for pkg in ("culmus-fonts-all", "google-noto-sans-hebrew-vf-fonts",
                "google-noto-serif-hebrew-vf-fonts", "google-noto-rashi-hebrew-vf-fonts",
                "sil-ezra-fonts"):
        assert pkg in CONTAINERFILE, pkg


def test_the_dconf_keyfiles_parse():
    for path in (DESKTOP, GREETER):
        configparser.ConfigParser().read_string(path.read_text())
