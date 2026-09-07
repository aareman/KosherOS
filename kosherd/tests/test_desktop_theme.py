"""The wizard, the login screen and the desktop should share one look.

The first hands-on session flagged the jump from a light first-boot wizard
to a dark GDM login screen. The greeter runs on its own dconf profile, so
the desktop's scheme does not reach it; each surface is set to the light
"airy cloud" scheme explicitly here.
"""

import configparser
from pathlib import Path

FILES = Path(__file__).parents[2] / "os-image/files"
GREETER = FILES / "etc/dconf/db/gdm.d/10-kosheros-greeter"
DESKTOP = FILES / "etc/dconf/db/local.d/10-kosheros-branding"


def _dconf(path: Path) -> configparser.ConfigParser:
    cp = configparser.ConfigParser()
    cp.read_string(path.read_text())
    return cp


def test_the_login_screen_is_light():
    cp = _dconf(GREETER)
    assert cp["org/gnome/desktop/interface"]["color-scheme"] == "'default'"


def test_the_desktop_default_is_light_too():
    cp = _dconf(DESKTOP)
    assert cp["org/gnome/desktop/interface"]["color-scheme"] == "'default'", \
        "desktop should match the light wizard and greeter"


def test_the_desktop_scheme_is_not_locked():
    # A preference, not identity: a user may switch to dark. Only the login
    # brand is locked.
    locks = (FILES / "etc/dconf/db/local.d/locks").glob("*")
    for lock in locks:
        assert "color-scheme" not in lock.read_text()


def test_both_dconf_keyfiles_parse():
    # dconf update would reject a malformed keyfile at build; parse here so
    # a typo fails the test suite first.
    for path in (GREETER, DESKTOP):
        _dconf(path)  # raises on malformed content


def test_firefox_trusts_the_system_ca_store():
    # Firefox keeps its own trust store; without this it does not trust the
    # proxy's inspection CA (which lives in the system store), so every
    # intercepted HTTPS page faults and filtering appears broken.
    import json

    pol = json.loads((FILES / "etc/firefox/policies/policies.json").read_text())
    assert pol["policies"]["Certificates"]["ImportEnterpriseRoots"] is True
