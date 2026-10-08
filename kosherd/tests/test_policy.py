import json
from pathlib import Path

import pytest

from kosherd import policy as policy_mod
from kosherd.policy import Policy, PolicyError, UserPolicy

EXAMPLE = Path(__file__).parents[2] / "policy" / "examples" / "family.json"


def example_policy() -> Policy:
    return Policy.from_dict(json.loads(EXAMPLE.read_text()))


def test_example_roundtrip():
    pol = example_policy()
    assert pol.revision == 3
    assert pol.user(1002).mode == "whitelist"
    assert "chinuch.org" in pol.user(1002).whitelist
    assert pol.guardian_enabled
    # to_dict output must itself validate
    assert Policy.from_dict(pol.to_dict()).to_dict() == pol.to_dict()


def test_rejects_bad_mode():
    doc = example_policy().to_dict()
    doc["users"][0]["mode"] = "open"
    with pytest.raises(PolicyError):
        Policy.from_dict(doc)


def test_rejects_system_uid():
    doc = example_policy().to_dict()
    doc["users"][0]["uid"] = 0
    with pytest.raises(PolicyError):
        Policy.from_dict(doc)


def test_rejects_bad_domain():
    doc = example_policy().to_dict()
    doc["users"][2]["whitelist"] = ["not a domain!"]
    with pytest.raises(PolicyError):
        Policy.from_dict(doc)


def test_system_whitelist_merges_builtins():
    pol = example_policy()
    eff = pol.effective_system_whitelist()
    assert "nmcheck.gnome.org" in eff
    assert "portal.example.org" in eff


def test_save_and_load_bumps_revision(tmp_path):
    path = tmp_path / "policy.json"
    pol = example_policy()
    policy_mod.save(pol, path)
    assert pol.revision == 4
    loaded = policy_mod.load(path)
    assert loaded.to_dict() == pol.to_dict()
    assert (path.stat().st_mode & 0o777) == 0o600


def test_load_missing_returns_empty(tmp_path):
    pol = policy_mod.load(tmp_path / "nope.json")
    assert pol.users == [] and pol.revision == 0


def test_empty_policy_is_valid():
    Policy.from_dict(Policy().to_dict())


def test_user_lookup_missing():
    assert example_policy().user(4242) is None


def test_user_policy_to_dict_omits_defaults():
    d = UserPolicy(uid=1000, username="x", mode="none").to_dict()
    assert d == {"uid": 1000, "username": "x", "mode": "none"}


# -- media and YouTube --------------------------------------------------------

def test_media_level_defaults_to_showing_everything():
    from kosherd.policy import DEFAULT_MEDIA_LEVEL, UserPolicy

    user = UserPolicy(uid=1000, username="x", mode="filtered")
    assert user.media_level == DEFAULT_MEDIA_LEVEL == "none"
    assert "media_level" not in user.to_dict()  # omitted when default


def test_media_level_roundtrips():
    from kosherd.policy import UserPolicy

    pol = Policy(users=[UserPolicy(uid=1000, username="x", mode="filtered",
                                   media_level="immodest")])
    assert Policy.from_dict(pol.to_dict()).users[0].media_level == "immodest"


def test_media_levels_run_from_permissive_to_strict():
    from kosherd.policy import MEDIA_LEVELS

    # Order matters: enforcement compares positions to decide what to hide.
    assert MEDIA_LEVELS.index("none") < MEDIA_LEVELS.index("nsfw")
    assert MEDIA_LEVELS.index("nsfw") < MEDIA_LEVELS.index("suggestive")
    assert MEDIA_LEVELS.index("suggestive") < MEDIA_LEVELS.index("immodest")
    assert MEDIA_LEVELS.index("immodest") < MEDIA_LEVELS.index("all")


def test_youtube_settings_roundtrip():
    from kosherd.policy import UserPolicy

    yt = {"blocked_categories": ["24"], "allowed_channels": ["@torah"],
          "restrict": "strict"}
    pol = Policy(users=[UserPolicy(uid=1000, username="x", mode="filtered",
                                   youtube=yt)])
    assert Policy.from_dict(pol.to_dict()).users[0].youtube == yt


def test_an_approved_channel_found_by_search_keeps_its_name():
    # Listed by ID, which is what every YouTube page names; the name is
    # only for the admin app to show.
    yt = {"allowed_channels": ["UC7BFmSXP4mHMNSvWUaqg2uQ"],
          "channel_names": {"UC7BFmSXP4mHMNSvWUaqg2uQ": "TorahAnytime"}}
    pol = Policy(users=[UserPolicy(uid=1000, username="x", mode="filtered", youtube=yt)])
    assert Policy.from_dict(pol.to_dict()).users[0].youtube == yt
    doc = pol.to_dict()
    doc["users"][0]["youtube"]["channel_names"] = {"UC7BFmSXP4mHMNSvWUaqg2uQ": 7}
    with pytest.raises(PolicyError):
        Policy.from_dict(doc)


def test_an_invalid_media_level_is_rejected():
    from kosherd.policy import UserPolicy

    pol = Policy(users=[UserPolicy(uid=1000, username="x", mode="filtered")])
    doc = pol.to_dict()
    doc["users"][0]["media_level"] = "somewhat"
    with pytest.raises(PolicyError):
        Policy.from_dict(doc)


# -- the guest account gets the same settings as anyone else ------------------

def test_the_guest_carries_picture_language_and_youtube_settings():
    # Without these the guest was the one account with no picture
    # filtering, no language filtering and no YouTube limits whatever the
    # household had chosen — a hole in exactly the account nobody watches.
    from kosherd.policy import GuestPolicy, Policy

    policy = Policy(revision=1, users=[], guest=GuestPolicy(
        enabled=True, uid=1010, mode="filtered",
        blocked_categories=["adult"], media_level="immodest",
        language_filter="substitute", youtube={"restrict": "strict"}))
    guest = next(u for u in policy.effective_users() if u.uid == 1010)
    assert guest.media_level == "immodest"
    assert guest.language_filter == "substitute"
    assert guest.youtube == {"restrict": "strict"}
    assert guest.blocked_categories == ["adult"]


def test_guest_settings_survive_a_round_trip():
    from kosherd.policy import GuestPolicy, Policy

    policy = Policy(revision=1, users=[], guest=GuestPolicy(
        enabled=True, uid=1010, mode="filtered", media_level="all",
        language_filter="block", youtube={"blocked_categories": ["24"]}))
    again = Policy.from_dict(policy.to_dict())
    assert again.guest.media_level == "all"
    assert again.guest.language_filter == "block"
    assert again.guest.youtube == {"blocked_categories": ["24"]}


def test_a_guest_with_no_settings_reads_as_unfiltered_pictures():
    from kosherd.policy import GuestPolicy, Policy

    policy = Policy(revision=1, users=[],
                    guest=GuestPolicy(enabled=True, uid=1010, mode="whitelist"))
    guest = next(u for u in policy.effective_users() if u.uid == 1010)
    assert guest.media_level == "none"
    assert guest.language_filter == "off"
    # ...and an untouched guest does not bloat the saved policy.
    assert set(policy.to_dict()["guest"]) == {"enabled", "uid"}


def test_usernames_may_have_capitals_and_dots_like_useradd_allows():
    # "Elisha" was created by accountsservice and then refused by the
    # schema, which left a half-made account. The rule is now what the OS
    # accepts.
    from kosherd import policy as policy_mod

    for name in ("elisha", "Elisha", "elisha.b", "_svc", "chaya-sara"):
        pol = Policy(users=[UserPolicy(uid=1000, username=name, mode="filtered")])
        policy_mod.validate(pol.to_dict())
    import pytest

    for name in ("9lives", "eli sha", ""):
        pol = Policy(users=[UserPolicy(uid=1000, username=name, mode="filtered")])
        with pytest.raises(Exception):
            policy_mod.validate(pol.to_dict())

