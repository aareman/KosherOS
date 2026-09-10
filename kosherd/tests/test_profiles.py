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
                      language_filter=profile.language_filter,
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
                      language_filter=profile.language_filter,
                      youtube=dict(profile.youtube))
    assert matching(user) is None


def test_a_profile_is_recognised_from_the_dict_form_too():
    # The admin app and the portal hold users as plain dicts, not objects.
    profile = get("adult")
    user = {"uid": 1001, "username": "a", "mode": profile.mode,
            "blocked_categories": list(profile.blocked_categories),
            "media_level": profile.media_level,
            "language_filter": profile.language_filter,
            "youtube": dict(profile.youtube),
            "can_install_apps": profile.can_install_apps}
    assert matching(user) == "adult"
    user["media_level"] = "none"
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


# -- what a new account starts out as -----------------------------------------

def test_a_new_account_can_be_created_straight_from_a_profile():
    # Creating an account with a bare filter mode and nothing else means
    # eight more settings to find, which is how accounts end up half
    # configured. The daemon accepts a profile key wherever it accepts a
    # mode.
    from kosherd.daemon import Daemon

    user = Daemon._new_user(1001, "child1", "child")
    profile = get("child")
    assert user.mode == profile.mode
    assert user.media_level == profile.media_level
    assert user.language_filter == profile.language_filter
    assert sorted(user.blocked_categories) == sorted(profile.blocked_categories)
    assert user.can_install_apps == profile.can_install_apps
    assert matching(user) == "child"


def test_a_bare_filter_mode_still_works():
    from kosherd.daemon import Daemon

    user = Daemon._new_user(1001, "someone", "dnsfilter")
    assert user.mode == "dnsfilter"
    assert user.media_level == "none"


# -- how far an account has drifted from its preset -----------------------------

def test_an_account_on_a_preset_has_no_drift():
    from kosherd.profiles import diff

    profile = get("child")
    user = {"mode": profile.mode,
            "blocked_categories": list(profile.blocked_categories),
            "media_level": profile.media_level,
            "language_filter": profile.language_filter,
            "youtube": dict(profile.youtube),
            "can_install_apps": profile.can_install_apps}
    assert diff(user) == ("child", [])


def test_one_extra_category_is_one_named_change_not_custom():
    from kosherd.profiles import diff

    profile = get("child")
    user = {"mode": profile.mode,
            "blocked_categories": [*profile.blocked_categories, "sports"],
            "media_level": profile.media_level,
            "language_filter": profile.language_filter,
            "youtube": dict(profile.youtube),
            "can_install_apps": profile.can_install_apps}
    key, changes = diff(user)
    assert key == "child"
    assert changes == [{"field": "blocked_categories", "added": ["sports"],
                        "removed": []}]


def test_the_nearest_preset_wins_and_every_departure_is_listed():
    from kosherd.profiles import diff

    teen = get("teen")
    user = {"mode": teen.mode,
            "blocked_categories": list(teen.blocked_categories),
            "media_level": "all",              # stricter than teen
            "language_filter": teen.language_filter,
            "youtube": {"restrict": "strict"},  # stricter than teen
            "can_install_apps": teen.can_install_apps}
    key, changes = diff(user)
    assert key == "teen"
    assert {c["field"] for c in changes} == {"media_level", "youtube"}
    media = next(c for c in changes if c["field"] == "media_level")
    assert (media["from"], media["to"]) == ("immodest", "all")


def test_an_account_in_a_mode_no_preset_uses_has_no_nearest_preset():
    from kosherd.profiles import diff

    assert diff({"mode": "none", "blocked_categories": []}) == (None, [])


def test_a_families_own_preset_can_be_the_nearest():
    from kosherd.profiles import diff, from_user

    base = get("teen")
    tuned = {"mode": base.mode,
             "blocked_categories": [*base.blocked_categories, "games", "sports"],
             "media_level": base.media_level, "language_filter": "block",
             "youtube": dict(base.youtube), "can_install_apps": False}
    custom = [from_user(tuned, "Yeshiva bochur")]
    assert diff(tuned, custom) == ("custom-yeshiva-bochur", [])
    tuned["blocked_categories"].remove("games")
    key, changes = diff(tuned, custom)
    assert key == "custom-yeshiva-bochur"
    assert changes[0]["removed"] == ["games"]

