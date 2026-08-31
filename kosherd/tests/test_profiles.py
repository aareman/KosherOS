"""Profiles: one choice per child instead of twenty switches."""

import pytest

from kosherd.policy import MEDIA_LEVELS, MODES, UserPolicy
from kosherd.profiles import BY_KEY, DEFAULT_PROFILE, PROFILES, describe, get, matching


def test_every_profile_is_internally_valid():
    # A profile that sets a mode or level the system does not know would
    # fail only when a parent selected it.
    for p in PROFILES:
        assert p.mode in MODES, p.key
        assert p.media_level in MEDIA_LEVELS, p.key
        assert p.language_filter in ("off", "substitute", "block"), p.key
        assert p.label and p.description, p.key


def test_the_default_profile_exists_and_filters():
    profile = get(DEFAULT_PROFILE)
    assert profile.mode == "filtered"
    assert "adult" in profile.blocked_categories


def test_profiles_run_from_most_to_least_protected():
    keys = [p.key for p in PROFILES]
    assert keys.index("young_child") < keys.index("child")
    assert keys.index("child") < keys.index("teen")
    assert keys.index("teen") < keys.index("adult")
    assert keys[-1] == "unfiltered"


def test_stricter_profiles_block_at_least_as_much():
    child = set(get("child").blocked_categories)
    teen = set(get("teen").blocked_categories)
    adult = set(get("adult").blocked_categories)
    assert teen <= child, "a teenager should not be blocked from more than a child"
    assert adult <= teen


def test_the_young_child_profile_needs_no_judgement_calls():
    # Whitelist plus no web images: nothing here depends on a classifier
    # being right, which is what makes it safe for the youngest users.
    profile = get("young_child")
    assert profile.mode == "whitelist"
    assert profile.media_level == "all"
    assert profile.can_install_apps is False


def test_the_unfiltered_profile_really_filters_nothing():
    profile = get("unfiltered")
    assert profile.mode == "unfiltered"
    assert profile.blocked_categories == ()
    assert profile.media_level == "none"
    assert profile.language_filter == "off"


def test_a_user_created_from_a_profile_is_recognised_as_that_profile():
    profile = get("teen")
    user = UserPolicy(uid=1001, username="t", mode=profile.mode,
                      blocked_categories=list(profile.blocked_categories),
                      media_level=profile.media_level,
                      youtube=dict(profile.youtube),
                      can_install_apps=profile.can_install_apps)
    assert matching(user) == "teen"


def test_a_customised_user_reports_no_profile():
    # Telling the truth matters: showing "Teenager" for settings that are no
    # longer the teenager profile would mislead the person relying on it.
    profile = get("teen")
    user = UserPolicy(uid=1001, username="t", mode=profile.mode,
                      blocked_categories=["adult"],  # narrowed by hand
                      media_level=profile.media_level,
                      youtube=dict(profile.youtube))
    assert matching(user) is None


def test_describe_is_serialisable_for_the_ui():
    import json

    described = describe()
    assert len(described) == len(PROFILES)
    json.dumps(described)  # must survive the D-Bus/JSON trip
    assert {"key", "label", "description", "mode"} <= set(described[0])


def test_unknown_profile_is_an_error():
    with pytest.raises(KeyError):
        get("nonexistent")
