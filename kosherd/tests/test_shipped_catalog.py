"""The shelf a family finds on day one.

The image ships an approved-app list so that an ordinary family, a student
and a developer all find what they need without anybody curating anything
(the standing "complete out of the box" rule). These check it is coherent:
every entry is shelvable by the Store, the refs are real Flathub ids, and
the categories a family actually needs are represented.
"""

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
CATALOG = ROOT / "os-image/files/etc/kosher/catalog.json"
APPS = json.loads(CATALOG.read_text())["apps"]

# The Store's shelves, read from its source so the two cannot drift.
STORE = (ROOT / "store-app/src/kosherstore/app.py").read_text()
SHELF_CLAIMS = {
    key: set(re.findall(r'"([A-Za-z]+)"', claims))
    for key, claims in re.findall(r'\("([a-z]+)", "[^"]+", \(([^)]*)\)\)', STORE)
}


def shelf_of(app):
    for key, claims in SHELF_CLAIMS.items():
        if set(app.get("categories") or ()) & claims:
            return key
    return "other"


def test_every_entry_has_what_the_store_draws():
    for app in APPS:
        assert app["ref"] and app["name"] and app["summary"], app
        assert app.get("categories"), f"{app['ref']} has no categories to shelve it by"


def test_refs_look_like_flatpak_app_ids_and_are_unique():
    refs = [a["ref"] for a in APPS]
    assert len(refs) == len(set(refs))
    for ref in refs:
        assert re.fullmatch(r"[A-Za-z][\w-]*(\.[A-Za-z0-9][\w-]*){2,}", ref), ref


def test_nothing_falls_off_the_shelves():
    unshelved = [a["ref"] for a in APPS if shelf_of(a) == "other"]
    assert not unshelved, unshelved


def test_a_family_a_student_and_a_developer_all_find_something():
    counts: dict[str, int] = {}
    for app in APPS:
        counts[shelf_of(app)] = counts.get(shelf_of(app), 0) + 1
    for shelf, least in (("internet", 3), ("work", 4), ("pictures", 6),
                         ("music", 3), ("develop", 4), ("games", 3),
                         ("learning", 2), ("utilities", 4)):
        assert counts.get(shelf, 0) >= least, f"{shelf}: {counts.get(shelf, 0)}"


@pytest.mark.parametrize("ref", [
    "org.mozilla.firefox", "org.chromium.Chromium",          # browsers
    "org.videolan.VLC", "org.kde.kdenlive", "io.bassi.Amberol",  # media
    "org.libreoffice.LibreOffice", "org.mozilla.Thunderbird",     # productivity
    "com.visualstudio.code", "org.gnome.Builder", "dev.zed.Zed",  # development
])
def test_the_apps_people_ask_for_by_name_are_there(ref):
    assert any(a["ref"] == ref for a in APPS)


def test_the_catalog_is_the_allowlist_and_says_so():
    assert "ONLY these refs" in json.loads(CATALOG.read_text())["comment"]
