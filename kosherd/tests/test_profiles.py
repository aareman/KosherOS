"""Groups and defaults: what an account is set up as, without shipped presets.

No presets ship. "Child" and "Teenager" are labels that mean different
things in different homes, so a family makes its own groups; what ships is
a complete, strict default for an account in no group, and complete
settings for each kind of internet.
"""

import pytest

from kosherd import profiles
from kosherd.policy import MEDIA_LEVELS, MODES, UserPolicy
from kosherd.profiles import DEFAULTS, MODE_DEFAULTS, describe, diff, for_mode, get


def test_nothing_ready_made_ships():
    assert profiles.PROFILES == ()
    assert describe() == []
    assert profiles.all_profiles() == ()


def test_the_defaults_are_strict_and_complete():
    # The account nobody configured must still be properly protected.
    assert DEFAULTS.mode == "filtered"
    assert {"adult", "social", "video", "immodest"} <= set(DEFAULTS.blocked_categories)
    assert DEFAULTS.media_level == "immodest"
    assert DEFAULTS.language_filter == "substitute"
    assert DEFAULTS.youtube["restrict"] == "strict"
    assert "shorts" in DEFAULTS.youtube["blocked_categories"]
    assert DEFAULTS.can_install_apps is False
    assert DEFAULTS.key == "" and DEFAULTS.label == "", "the default is not a group"


def test_every_kind_of_internet_has_complete_settings():
    assert set(MODE_DEFAULTS) == set(MODES)
    for mode, p in MODE_DEFAULTS.items():
        assert p.mode == mode
        assert p.media_level in MEDIA_LEVELS, mode
        assert p.language_filter in ("off", "substitute", "block"), mode
        assert p.key == "" and p.label == "", "defaults carry no name"
    assert for_mode("filtered") is DEFAULTS


def test_the_approved_sites_default_needs_no_judgement_calls():
    # Whitelist plus no web images: nothing depends on a classifier being
    # right, which is what makes it safe for the youngest users.
    p = for_mode("whitelist")
    assert p.media_level == "all" and p.can_install_apps is False


def test_the_unfiltered_default_really_filters_nothing():
    p = for_mode("unfiltered")
    assert p.blocked_categories == () and p.media_level == "none"
    assert p.language_filter == "off"


def test_unknown_group_is_an_error():
    with pytest.raises(KeyError):
        get("nonexistent")


# -- what a new account starts out as -----------------------------------------

def test_a_new_account_in_no_group_gets_the_defaults():
    from kosherd.daemon import Daemon

    user = Daemon._new_user(1001, "someone", "filtered")
    assert user.profile is None
    assert user.media_level == DEFAULTS.media_level
    assert user.language_filter == DEFAULTS.language_filter
    assert sorted(user.blocked_categories) == sorted(DEFAULTS.blocked_categories)
    assert user.can_install_apps is False


def test_the_first_administrator_gets_the_floor_and_the_rest_open():
    from kosherd.daemon import Daemon
    from kosherd.categories import DEFAULT_BLOCKED

    admin = Daemon._new_user(1000, "abba", "filtered", admin=True)
    assert set(admin.blocked_categories) == set(DEFAULT_BLOCKED)
    assert admin.can_install_apps is True and admin.profile is None


def test_a_new_account_by_kind_of_internet_gets_that_kinds_settings():
    from kosherd.daemon import Daemon

    user = Daemon._new_user(1001, "someone", "dnsfilter")
    assert user.mode == "dnsfilter" and user.media_level == "none"
    little = Daemon._new_user(1002, "little", "whitelist")
    assert little.mode == "whitelist" and little.media_level == "all"


def test_a_new_account_can_be_created_straight_into_a_group():
    from kosherd.daemon import Daemon

    group = profiles.to_dict(profiles.from_user(
        {"mode": "filtered", "blocked_categories": ["adult", "games"],
         "media_level": "all", "language_filter": "block", "youtube": {},
         "can_install_apps": True}, "Kids"))
    user = Daemon._new_user(1001, "child1", "custom-kids", [group])
    assert user.profile == "custom-kids"
    assert user.media_level == "all" and user.language_filter == "block"
    assert sorted(user.blocked_categories) == ["adult", "games"]


# -- how far an account has drifted from its group -----------------------------

def _kids():
    return [profiles.to_dict(profiles.Profile(
        key="custom-kids", label="Kids", description="", mode="filtered",
        blocked_categories=("adult", "gambling"), media_level="immodest",
        language_filter="substitute", youtube={"restrict": "strict"},
        can_install_apps=False))]


def test_membership_is_explicit_not_guessed_from_the_settings():
    # An account with exactly a group's settings but no membership is in no
    # group; one in the group with different settings is still in it.
    same = {"mode": "filtered", "blocked_categories": ["adult", "gambling"],
            "media_level": "immodest", "language_filter": "substitute",
            "youtube": {"restrict": "strict"}, "can_install_apps": False}
    assert diff(same, _kids()) == (None, [])
    member = {**same, "profile": "custom-kids", "blocked_categories": ["adult"]}
    key, changes = diff(member, _kids())
    assert key == "custom-kids"
    assert changes == [{"field": "blocked_categories", "added": [], "removed": ["gambling"]}]


def test_an_account_on_its_group_has_no_drift():
    user = UserPolicy(uid=1, username="y", mode="filtered", profile="custom-kids",
                      blocked_categories=["adult", "gambling"], media_level="immodest",
                      language_filter="substitute", youtube={"restrict": "strict"},
                      can_install_apps=False)
    assert diff(user, _kids()) == ("custom-kids", [])


def test_every_departure_is_listed_including_the_mode():
    user = {"mode": "dnsfilter", "profile": "custom-kids",
            "blocked_categories": ["adult", "gambling"], "media_level": "all",
            "language_filter": "substitute", "youtube": {"restrict": "none"},
            "can_install_apps": False}
    key, changes = diff(user, _kids())
    assert key == "custom-kids"
    assert {c["field"] for c in changes} == {"mode", "media_level", "youtube"}


def test_an_account_whose_group_was_deleted_is_in_none():
    assert diff({"mode": "filtered", "profile": "custom-gone"}, _kids()) == (None, [])


def test_members_are_found_by_membership():
    users = [UserPolicy(uid=1, username="a", mode="filtered", profile="custom-kids"),
             UserPolicy(uid=2, username="b", mode="filtered"),
             {"uid": 3, "username": "c", "mode": "filtered", "profile": "custom-kids"}]
    assert [profiles._field(u, "uid") for u in profiles.members(users, "custom-kids")] == [1, 3]
