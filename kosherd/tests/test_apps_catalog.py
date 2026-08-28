"""The approved-app allowlist: catalog, install ledger, remote filter.

The catalog IS the security boundary for app installs, so these cover the
mutations and the rendered filter rather than just the happy path.
"""

import json

import pytest

from kosherd import apps
from kosherd.apps import AppError


@pytest.fixture
def catalog(tmp_path, monkeypatch):
    """Point the module at a temporary catalog, with no remote to apply to."""
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps({"apps": [
        {"ref": "org.mozilla.firefox", "name": "Firefox", "summary": "Web browser"},
    ]}))
    monkeypatch.setattr(apps, "CATALOG_PATH", path)
    monkeypatch.setattr(apps, "FILTER_PATH", tmp_path / "flathub.filter")
    monkeypatch.setattr(apps, "LEDGER_PATH", tmp_path / "installs.json")
    # write_remote_filter() also pushes the filter to flatpak; skip that half.
    monkeypatch.setattr(apps, "write_remote_filter", lambda: None)
    return path


# -- catalog -----------------------------------------------------------------

def test_load_and_allowed_refs(catalog):
    assert apps.allowed_refs() == {"org.mozilla.firefox"}


def test_missing_catalog_is_empty_not_an_error(catalog, tmp_path, monkeypatch):
    monkeypatch.setattr(apps, "CATALOG_PATH", tmp_path / "absent.json")
    assert apps.load_catalog() == {"apps": []}
    assert apps.allowed_refs() == set()


def test_corrupt_catalog_is_reported_clearly(catalog):
    catalog.write_text("{not json")
    with pytest.raises(AppError, match="not valid JSON"):
        apps.load_catalog()


def test_approve_adds_and_sorts(catalog):
    apps.approve("org.gimp.GIMP", "GIMP", "Image editing")
    names = [a["name"] for a in apps.load_catalog()["apps"]]
    assert names == ["Firefox", "GIMP"]
    entry = next(a for a in apps.load_catalog()["apps"] if a["ref"] == "org.gimp.GIMP")
    assert entry["summary"] == "Image editing"


def test_approve_falls_back_to_the_ref_as_a_name(catalog):
    apps.approve("org.x.Thing")
    entry = next(a for a in apps.load_catalog()["apps"] if a["ref"] == "org.x.Thing")
    assert entry["name"] == "org.x.Thing"
    assert "summary" not in entry


def test_approving_twice_is_refused(catalog):
    with pytest.raises(AppError, match="already approved"):
        apps.approve("org.mozilla.firefox", "Firefox")


def test_unapprove_removes(catalog):
    apps.unapprove("org.mozilla.firefox")
    assert apps.allowed_refs() == set()


def test_unapproving_something_unapproved_is_refused(catalog):
    with pytest.raises(AppError, match="not on the approved list"):
        apps.unapprove("org.videolan.VLC")


def test_catalog_writes_are_atomic(catalog, tmp_path):
    apps.approve("org.gimp.GIMP", "GIMP")
    # No partial temp file left behind for the daemon to read on restart.
    assert not (tmp_path / "catalog.tmp").exists()
    json.loads(catalog.read_text())  # still valid JSON


# -- remote filter -----------------------------------------------------------

def test_filter_denies_by_default_and_allows_runtimes():
    rendered = apps.render_filter({"org.mozilla.firefox"})
    # A filter with any allow line makes deny the default; runtimes must be
    # allowed or approved apps cannot pull their dependencies.
    assert "allow runtime/*" in rendered
    assert "allow app/org.mozilla.firefox/*/*" in rendered


def test_filter_lists_only_approved_apps():
    rendered = apps.render_filter({"org.a.A", "org.b.B"})
    app_lines = [ln for ln in rendered.splitlines() if ln.startswith("allow app/")]
    assert app_lines == ["allow app/org.a.A/*/*", "allow app/org.b.B/*/*"]


def test_empty_catalog_still_allows_runtimes_only():
    app_lines = [ln for ln in apps.render_filter(set()).splitlines()
                 if ln.startswith("allow app/")]
    assert app_lines == []


# -- install ledger ----------------------------------------------------------

def test_ledger_records_who_installed_what(catalog):
    apps.record_install("org.gimp.GIMP", 1001, "kid1")
    ledger = json.loads(apps.LEDGER_PATH.read_text())
    assert ledger["org.gimp.GIMP"] == {"uid": 1001, "username": "kid1"}


def test_ledger_forgets_on_removal(catalog):
    apps.record_install("org.gimp.GIMP", 1001, "kid1")
    apps.forget_install("org.gimp.GIMP")
    assert json.loads(apps.LEDGER_PATH.read_text()) == {}


def test_forgetting_an_unknown_app_is_harmless(catalog):
    apps.forget_install("org.never.Installed")  # must not raise


def test_reinstall_overwrites_the_previous_installer(catalog):
    apps.record_install("org.gimp.GIMP", 1001, "kid1")
    apps.record_install("org.gimp.GIMP", 1000, "abba")
    ledger = json.loads(apps.LEDGER_PATH.read_text())
    assert ledger["org.gimp.GIMP"]["username"] == "abba"


def test_missing_or_corrupt_ledger_reads_as_empty(catalog):
    assert apps._load_ledger() == {}
    apps.LEDGER_PATH.write_text("{broken")
    assert apps._load_ledger() == {}


# -- the shipped catalog -----------------------------------------------------

def test_shipped_catalog_entries_are_well_formed():
    from pathlib import Path

    path = (Path(__file__).parents[2] / "os-image" / "files" / "etc" / "kosher"
            / "catalog.json")
    entries = json.loads(path.read_text())["apps"]
    refs = [a["ref"] for a in entries]
    assert refs and len(refs) == len(set(refs)), "duplicate refs in the catalog"
    for entry in entries:
        assert entry.get("name"), f"{entry['ref']} has no display name"
        # flatpak application ids are reverse-DNS; a typo here ships an app
        # nobody can install (kosherctl check-catalog verifies against the
        # real remote, which needs a network and so is not a unit test).
        assert entry["ref"].count(".") >= 2, f"{entry['ref']} is not an app id"
