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


def test_an_invalid_media_level_is_rejected():
    from kosherd.policy import UserPolicy

    pol = Policy(users=[UserPolicy(uid=1000, username="x", mode="filtered")])
    doc = pol.to_dict()
    doc["users"][0]["media_level"] = "somewhat"
    with pytest.raises(PolicyError):
        Policy.from_dict(doc)
