"""The admin app must have words for every setting the daemon accepts.

Adding a media level or a YouTube category and forgetting the UI leaves a
dropdown that silently cannot reach it, or — worse — one whose entries no
longer line up with the values behind them. The admin app needs GTK to
import, which the test environment does not have, so the label tables are
read out of the source instead.
"""

import ast
from pathlib import Path

import pytest

from kosherd.language import MODES as LANGUAGE_MODES
from kosherd.policy import COVER_STYLES, LAYOUTS, MEDIA_LEVELS, MODES, YOUTUBE_CATEGORIES

APP = Path(__file__).parents[2] / "admin-app/src/kosheradmin/labels.py"


@pytest.fixture(scope="module")
def constants():
    """Top-level literal assignments in the admin app."""
    tree = ast.parse(APP.read_text())
    found = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 \
                and isinstance(node.targets[0], ast.Name):
            try:
                found[node.targets[0].id] = ast.literal_eval(node.value)
            except ValueError:
                continue
    return found


def test_every_filter_mode_has_a_label_and_a_hint(constants):
    assert set(constants["MODE_LABELS"]) == set(MODES)
    assert set(constants["MODE_HINTS"]) == set(MODES)


def test_every_media_level_has_a_label_and_a_hint(constants):
    assert set(constants["MEDIA_LABELS"]) == set(MEDIA_LEVELS)
    assert set(constants["MEDIA_HINTS"]) == set(MEDIA_LEVELS)


def test_every_language_setting_has_a_label(constants):
    assert set(constants["LANGUAGE_LABELS"]) == set(LANGUAGE_MODES)
    # The dropdown is built from the order tuple, so it must be complete
    # AND in a fixed order — the index is what selects the value.
    assert set(constants["LANGUAGE_ORDER"]) == set(LANGUAGE_MODES)


def test_every_desktop_layout_has_a_label_and_a_hint(constants):
    assert set(constants["LAYOUT_LABELS"]) == set(LAYOUTS)
    assert set(constants["LAYOUT_HINTS"]) == set(LAYOUTS)
    # The dropdown index selects the value, so the order tuple must be
    # complete; and the classic layout is first because it is the answer
    # for almost everyone.
    assert set(constants["LAYOUT_ORDER"]) == set(LAYOUTS)
    assert constants["LAYOUT_ORDER"][0] == "classic"


def test_every_cover_style_has_a_label_and_a_hint(constants):
    assert set(constants["COVER_LABELS"]) == set(COVER_STYLES)
    assert set(constants["COVER_HINTS"]) == set(COVER_STYLES)
    assert set(constants["COVER_ORDER"]) == set(COVER_STYLES)
    assert constants["COVER_ORDER"][0] == "frost", "the default comes first"


def test_the_youtube_restricted_mode_choices_match_the_daemon(constants):
    # The daemon rejects anything else (see impl_SetYouTube).
    assert set(constants["YOUTUBE_RESTRICT_LABELS"]) == {"none", "moderate", "strict"}
    assert set(constants["YOUTUBE_RESTRICT_ORDER"]) == {"none", "moderate", "strict"}


def test_youtube_categories_are_worth_showing():
    # Codes are YouTube's, so they must not be renumbered on a whim; the
    # labels are what a parent reads in the dialog.
    assert "27" in YOUTUBE_CATEGORIES and YOUTUBE_CATEGORIES["27"] == "Education"
    assert all(code.isdigit() and label for code, label in YOUTUBE_CATEGORIES.items())


def test_every_reportable_picture_state_has_something_to_say(constants):
    # A filter that has quietly stopped doing something is worse than one
    # that never did it. Adding a state and forgetting the words for it
    # means the app says nothing at all.
    from kosherd import vision

    reportable = {vision.TOO_SLOW, vision.NO_MODEL}
    assert set(constants["PICTURE_STATE"]) == reportable
    for title, body in constants["PICTURE_STATE"].values():
        assert title and body


def test_the_app_offers_exactly_the_lists_the_daemon_will_edit(constants):
    # A button for a list the daemon refuses is a dead end; a list the
    # daemon accepts with no button is a feature nobody can reach.
    from kosherd.daemon import Daemon

    offered = {entry[0] for entry in constants["EDITABLE_LISTS"]}
    assert offered == set(Daemon.EDITABLE_LISTS)


def test_the_content_levels_offered_are_ones_the_scorer_knows(constants):
    from kosherd import content

    for key, label, weight in constants["CONTENT_LEVELS"]:
        assert key in content.SEVERITY and key != content.CLEAN
        assert label and isinstance(weight, int)
    # Enough on its own to convict only at the explicit end, which is what
    # the dialog tells the person.
    by_key = {k: w for k, _l, w in constants["CONTENT_LEVELS"]}
    assert by_key["nsfw"] >= content.THRESHOLD
    assert by_key["immodest"] < content.THRESHOLD
