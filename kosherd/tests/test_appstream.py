"""Parsing Flathub's app metadata, and searching it.

Both bugs that shipped from this code are pinned here as regressions:
GIMP appearing under an Arabic name (which also wrecked search ranking),
and 47 apps — Firefox among them — being invisible because Flathub still
tags some components with AppStream's legacy type.
"""

import gzip

import pytest

from kosherd import apps

CATALOGUE = """<?xml version="1.0" encoding="UTF-8"?>
<components version="0.14">
  <component type="desktop-application">
    <id>org.gimp.GIMP.desktop</id>
    <name xml:lang="ar">برنامج جنو لمعالجة الصور</name>
    <name>GNU Image Manipulation Program</name>
    <name xml:lang="de">GNU-Bildbearbeitungsprogramm</name>
    <summary xml:lang="ar">تحرير الصور</summary>
    <summary>Create images and edit photographs</summary>
  </component>
  <component type="desktop">
    <id>org.mozilla.firefox</id>
    <name>Firefox</name>
    <summary>Web browser</summary>
  </component>
  <component type="console-application">
    <id>org.gnu.Hello</id>
    <name>Hello</name>
  </component>
  <component type="runtime">
    <id>org.freedesktop.Platform</id>
    <name>Freedesktop Platform</name>
  </component>
  <component type="addon">
    <id>org.gimp.GIMP.Plugin.Resynthesizer</id>
    <name>Resynthesizer</name>
  </component>
  <component type="localization">
    <id>org.kde.Platform.Locale</id>
    <name>KDE locales</name>
  </component>
  <component type="desktop-application">
    <id>org.chess.Chess</id>
    <name>Chess</name>
    <summary>Play chess</summary>
    <categories><category>Game</category><category>BoardGame</category></categories>
    <content_rating type="oars-1.1">
      <content_attribute id="violence-cartoon">none</content_attribute>
      <content_attribute id="violence-fantasy">mild</content_attribute>
      <content_attribute id="language-profanity">moderate</content_attribute>
    </content_rating>
  </component>
</components>
"""


# -- when to fetch the catalogue again -----------------------------------------

COMMIT = "a" * 64
OLD_FILTER = "b" * 64
NEW_FILTER = "c" * 64


def test_nothing_deployed_is_stale():
    assert apps.appstream_is_stale(None, NEW_FILTER, None)


def test_a_fresh_copy_under_the_current_filter_is_kept():
    assert not apps.appstream_is_stale(f"{COMMIT}-{NEW_FILTER}", NEW_FILTER, 3600)


def test_a_day_old_copy_is_fetched_again():
    assert apps.appstream_is_stale(f"{COMMIT}-{NEW_FILTER}", NEW_FILTER, 25 * 3600)


def test_a_copy_deployed_under_another_filter_is_stale_however_new():
    # flatpak cuts the apps a remote filter denies out of the appstream it
    # deploys, and names the directory <commit>-<filter checksum>. The
    # machine that upgraded from the allow-list filter had an index of the
    # fifty approved apps and nothing else, and it was going to keep it
    # for a day.
    assert apps.appstream_is_stale(f"{COMMIT}-{OLD_FILTER}", NEW_FILTER, 60)


def test_a_copy_deployed_with_no_filter_is_stale_once_there_is_one():
    assert apps.appstream_is_stale(COMMIT, NEW_FILTER, 60)
    assert not apps.appstream_is_stale(COMMIT, None, 60)


def test_the_filter_checksum_is_what_flatpak_computes(tmp_path):
    import hashlib

    path = tmp_path / "flathub.filter"
    path.write_text(apps.render_filter({"org.example.Tor"}))
    assert apps.filter_checksum(path) == hashlib.sha256(path.read_bytes()).hexdigest()
    assert apps.filter_checksum(tmp_path / "missing") is None


def test_ensure_appstream_reads_the_active_link_and_the_filter(tmp_path, monkeypatch):
    calls = []

    class FakeInstallation:
        @staticmethod
        def new_system(_c):
            return FakeInstallation()

        def update_appstream_sync(self, remote, arch, _c):
            calls.append((remote, arch))

    class FakeFlatpak:
        Installation = FakeInstallation

        @staticmethod
        def get_default_arch():
            return "x86_64"

    arch_dir = tmp_path / "flathub" / "x86_64"
    filter_path = tmp_path / "flathub.filter"
    filter_path.write_text(apps.render_filter(set()))
    checksum = apps.filter_checksum(filter_path)
    deployed = arch_dir / f"{COMMIT}-{checksum}"
    deployed.mkdir(parents=True)
    (deployed / "appstream.xml.gz").write_bytes(b"")
    (arch_dir / "active").symlink_to(deployed.name)
    monkeypatch.setattr(apps, "Flatpak", FakeFlatpak)
    monkeypatch.setattr(apps, "_appstream_dir", lambda: arch_dir)
    monkeypatch.setattr(apps, "FILTER_PATH", filter_path)

    apps.ensure_appstream()
    assert calls == [], "a fresh copy under the current filter is kept"
    filter_path.write_text(apps.render_filter({"org.example.Tor"}))
    apps.ensure_appstream()
    assert calls == [("flathub", "x86_64")], "a changed filter fetches again at once"
    apps.ensure_appstream(force=True)
    assert len(calls) == 2


def test_warm_index_says_when_the_copy_it_read_is_stale(monkeypatch):
    def failing():
        raise RuntimeError("no network")

    monkeypatch.setattr(apps, "ensure_appstream", failing)
    monkeypatch.setattr(apps, "_load_index", lambda: [{"ref": "a"}, {"ref": "b"}])
    assert apps.warm_index() == (2, False)
    monkeypatch.setattr(apps, "ensure_appstream", lambda: None)
    assert apps.warm_index() == (2, True)


def test_content_rating_is_kept_without_the_nones(catalogue):
    by_ref = {a["ref"]: a for a in apps.parse_appstream(catalogue)}
    assert by_ref["org.chess.Chess"]["rating"] == {
        "violence-fantasy": "mild", "language-profanity": "moderate"}
    assert by_ref["org.chess.Chess"]["categories"] == ["Game", "BoardGame"]
    # An app with no <content_rating> is unrated: an empty dict, never a
    # missing key, so the ceiling can be asked about every entry.
    assert by_ref["org.mozilla.firefox"]["rating"] == {}


@pytest.fixture
def catalogue(tmp_path):
    path = tmp_path / "appstream.xml.gz"
    with gzip.open(path, "wb") as fh:
        fh.write(CATALOGUE.encode())
    return path


def parsed(catalogue) -> dict[str, dict]:
    return {a["ref"]: a for a in apps.parse_appstream(catalogue)}


# -- regressions -------------------------------------------------------------

def test_english_name_wins_over_translations(catalogue):
    # The Arabic <name> comes first in the file; the untranslated one must
    # still be chosen, or apps show up in a random language and search
    # ranking breaks.
    assert parsed(catalogue)["org.gimp.GIMP"]["name"] == "GNU Image Manipulation Program"
    assert parsed(catalogue)["org.gimp.GIMP"]["summary"] == "Create images and edit photographs"


def test_legacy_desktop_component_type_is_included(catalogue):
    # Flathub still tags Firefox (and ~46 others) type="desktop".
    assert "org.mozilla.firefox" in parsed(catalogue)


def test_console_applications_are_included(catalogue):
    assert "org.gnu.Hello" in parsed(catalogue)


# -- filtering and shape -----------------------------------------------------

@pytest.mark.parametrize("ref", [
    "org.freedesktop.Platform",              # runtime
    "org.gimp.GIMP.Plugin.Resynthesizer",    # addon
    "org.kde.Platform.Locale",               # localization
])
def test_non_applications_are_excluded(catalogue, ref):
    assert ref not in parsed(catalogue)


def test_desktop_suffix_is_stripped_from_ids(catalogue):
    index = parsed(catalogue)
    assert "org.gimp.GIMP" in index and "org.gimp.GIMP.desktop" not in index


def test_missing_summary_becomes_empty(catalogue):
    assert parsed(catalogue)["org.gnu.Hello"]["summary"] == ""


def test_empty_catalogue_parses_to_nothing(tmp_path):
    path = tmp_path / "empty.xml.gz"
    with gzip.open(path, "wb") as fh:
        fh.write(b'<?xml version="1.0"?><components/>')
    assert apps.parse_appstream(path) == []


# -- search ------------------------------------------------------------------

@pytest.fixture
def searchable(catalogue, monkeypatch):
    """Search over the fixture catalogue, with everything 'available'."""
    index = sorted(apps.parse_appstream(catalogue), key=lambda a: a["name"].lower())
    monkeypatch.setattr(apps, "ensure_appstream", lambda: None)
    monkeypatch.setattr(apps, "_load_index", lambda: index)
    return index


def test_search_finds_apps_by_name(searchable):
    assert [a["ref"] for a in apps.search_remote("firefox")] == ["org.mozilla.firefox"]


def test_search_matches_id_and_summary(searchable):
    assert any(a["ref"] == "org.gnu.Hello" for a in apps.search_remote("gnu.Hello"))
    assert any(a["ref"] == "org.mozilla.firefox"
               for a in apps.search_remote("web browser"))


def test_exact_name_matches_rank_first(searchable):
    # The bug this guards: with names picked from a random language, an
    # exact match could rank below unrelated apps.
    assert apps.search_remote("chess")[0]["ref"] == "org.chess.Chess"


def test_prefix_matches_beat_substring_matches(searchable):
    results = [a["name"] for a in apps.search_remote("hell")]
    assert results and results[0] == "Hello"


def test_search_is_case_insensitive(searchable):
    assert apps.search_remote("FIREFOX")[0]["ref"] == "org.mozilla.firefox"


def test_empty_query_lists_everything_up_to_the_limit(searchable):
    assert len(apps.search_remote("")) == len(searchable)
    assert len(apps.search_remote("", limit=2)) == 2


def test_no_matches_returns_empty(searchable):
    assert apps.search_remote("nothing-like-this") == []
