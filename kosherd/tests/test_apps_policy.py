"""Allowlist behaviour that doesn't need libflatpak (import-guarded)."""

import json
from pathlib import Path

import pytest

from kosherd.policy import Policy, UserPolicy

EXAMPLE = Path(__file__).parents[2] / "policy" / "examples" / "family.json"
CATALOG = Path(__file__).parents[2] / "os-image" / "files" / "etc" / "kosher" / "catalog.json"


def test_can_install_defaults_true_and_roundtrips():
    pol = Policy.from_dict(json.loads(EXAMPLE.read_text()))
    assert all(u.can_install_apps for u in pol.users)
    pol.users[0].can_install_apps = False
    again = Policy.from_dict(pol.to_dict())
    assert again.users[0].can_install_apps is False
    assert all(u.can_install_apps for u in again.users[1:])


def test_can_install_omitted_when_true():
    d = UserPolicy(uid=1000, username="x", mode="none").to_dict()
    assert "can_install_apps" not in d


def test_shipped_catalog_is_valid():
    catalog = json.loads(CATALOG.read_text())
    refs = [a["ref"] for a in catalog["apps"]]
    assert refs, "catalog must not be empty"
    assert len(refs) == len(set(refs)), "duplicate refs in catalog"
    for app in catalog["apps"]:
        assert app.get("name"), f"{app['ref']} has no display name"
        # flatpak application ids are reverse-DNS
        assert app["ref"].count(".") >= 2, f"{app['ref']} is not a flatpak app id"


def test_apps_module_importable_where_flatpak_exists():
    # kosherd.apps needs the Flatpak GIR typelib, which the dev shell lacks;
    # in-VM verification covers the real install path.
    try:
        from kosherd import apps
    except (ImportError, ValueError) as e:
        pytest.skip(f"libflatpak typelib not available: {e}")
    assert apps.REMOTE == "flathub"
