"""The kinds of app: one vocabulary for the Store's shelves and the admin's
blocks, decided from Flathub's freedesktop categories."""

from kosherd import appkinds


def test_every_kind_has_a_label_a_colour_and_an_icon():
    for key in appkinds.KIND_KEYS:
        assert appkinds.KIND_LABELS[key]
        assert appkinds.KIND_TINTS[key].startswith("#")
        assert appkinds.KIND_ICONS[key]


def test_kind_keys_are_unique_and_include_other():
    assert len(set(appkinds.KIND_KEYS)) == len(appkinds.KIND_KEYS)
    assert appkinds.OTHER in appkinds.KIND_KEYS
    assert appkinds.is_kind("games") and not appkinds.is_kind("Game")


def test_no_category_is_claimed_by_two_kinds():
    seen: dict[str, str] = {}
    for key, _label, claims in appkinds.KINDS:
        for category in claims:
            assert category not in seen, f"{category} in both {seen[category]} and {key}"
            seen[category] = key


def test_a_browser_is_internet_and_a_puzzle_is_a_game():
    assert appkinds.kind_of({"categories": ["Network", "WebBrowser"]}) == "internet"
    assert appkinds.kind_of({"categories": ["Game", "LogicGame"]}) == "games"


def test_a_music_player_lands_on_music_not_pictures():
    # Every player carries AudioVideo; the specific category must win.
    assert appkinds.kind_of({"categories": ["AudioVideo", "Audio", "Music", "Player"]}) == "music"
    assert appkinds.kind_of({"categories": ["AudioVideo", "Video", "Player"]}) == "pictures"


def test_only_the_generic_media_categories_fall_to_pictures():
    assert appkinds.kind_of({"categories": ["AudioVideo"]}) == "pictures"
    assert appkinds.kind_of({"categories": ["AudioVideo", "Player"]}) == "pictures"


def test_nothing_known_is_everything_else():
    assert appkinds.kind_of({"ref": "x"}) == appkinds.OTHER
    assert appkinds.kind_of({"categories": []}) == appkinds.OTHER
    assert appkinds.kind_of({"categories": ["Nonsense"]}) == appkinds.OTHER
    assert appkinds.label(appkinds.OTHER) == "Everything else"
