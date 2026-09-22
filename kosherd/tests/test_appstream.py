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
