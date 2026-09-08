"""Save an account's current settings as a preset; apply it elsewhere.

The user's ask: "should be able to save current as a new preset". The
everyday path to a good preset is tuning one child's account until it is
right and reusing it — so presets are snapshots of accounts, live in the
policy (they persist and sync), and appear beside the built-ins.
"""

import json

import pytest

from kosherd import profiles
from kosherd.daemon import Daemon, PolicyError
from kosherd.policy import Policy, UserPolicy


def tuned_user(uid=1001):
    return UserPolicy(uid=uid, username="rivky", mode="filtered",
                      blocked_categories=["adult", "gambling", "social", "sports"],
                      media_level="immodest", language_filter="substitute",
                      youtube={"restrict": "strict", "allowed_channels": ["@torah"]},
                      can_install_apps=False)


def test_a_preset_is_a_snapshot_of_the_account():
    preset = profiles.from_user(tuned_user(), "Bais Yaakov girl", "For the girls")
    assert preset.key == "custom-bais-yaakov-girl"
    assert preset.mode == "filtered"
    assert preset.blocked_categories == ("adult", "gambling", "social", "sports")
    assert preset.media_level == "immodest"
    assert preset.youtube == {"restrict": "strict", "allowed_channels": ["@torah"]}
    assert preset.can_install_apps is False


def test_keys_are_slugs_that_cannot_collide_with_built_ins():
    assert profiles.slug("Teen") == "custom-teen"  # built-in is "teen"
    assert profiles.slug("  Yeshiva  Bochur!! ") == "custom-yeshiva-bochur"
    assert profiles.slug("???") == "custom-preset"
    assert all(k.startswith("custom-") is False for k in profiles.BY_KEY)


def test_custom_presets_appear_after_the_built_ins():
    custom = [profiles.to_dict(profiles.from_user(tuned_user(), "Mine"))]
    keys = [p.key for p in profiles.all_profiles(custom)]
    assert keys[:len(profiles.PROFILES)] == [p.key for p in profiles.PROFILES]
    assert keys[-1] == "custom-mine"
    assert profiles.get("custom-mine", custom).label == "Mine"
    with pytest.raises(KeyError):
        profiles.get("custom-mine")  # unknown without the family's list


def test_matching_recognises_a_custom_preset():
    user = tuned_user()
    custom = [profiles.to_dict(profiles.from_user(user, "Mine"))]
    assert profiles.matching(user) is None, "no built-in matches these settings"
    assert profiles.matching(user, custom) == "custom-mine"


def test_describe_marks_which_are_the_familys():
    custom = [profiles.to_dict(profiles.from_user(tuned_user(), "Mine"))]
    described = profiles.describe(custom)
    flags = {d["key"]: d["custom"] for d in described}
    assert flags["custom-mine"] is True
    assert flags["child"] is False


def test_presets_survive_the_policy_round_trip_and_the_schema():
    from kosherd.policy import validate

    user = tuned_user()
    policy = Policy(revision=3, users=[user],
                    custom_profiles=[profiles.to_dict(profiles.from_user(user, "Mine", "d"))])
    doc = json.loads(json.dumps(policy.to_dict()))
    assert doc["custom_profiles"][0]["key"] == "custom-mine"
    validate(doc)  # the shipped schema must accept it
    back = Policy.from_dict(doc)
    assert back.custom_profiles == policy.custom_profiles


def test_a_policy_without_presets_serialises_as_before():
    doc = Policy(revision=1, users=[tuned_user()]).to_dict()
    assert "custom_profiles" not in doc


# -- the daemon ------------------------------------------------------------------

def _daemon(users, custom=None):
    daemon = Daemon.__new__(Daemon)
    daemon.policy = Policy(revision=1, users=users,
                           custom_profiles=list(custom or []))
    daemon.connection = None
    daemon._saved = 0
    daemon._save_only = lambda: setattr(daemon, "_saved", daemon._saved + 1)
    daemon._save_and_apply = lambda: None
    return daemon


def test_saving_snapshots_the_account_and_persists_without_re_applying():
    daemon = _daemon([tuned_user()])
    key = daemon.impl_SaveProfile(1001, "Mine", "desc", "").unpack()[0]
    assert key == "custom-mine"
    assert daemon.policy.custom_profiles[0]["label"] == "Mine"
    assert daemon._saved == 1, "a preset changes no enforcement; save only"


def test_saving_the_same_name_replaces_the_preset():
    daemon = _daemon([tuned_user()])
    daemon.impl_SaveProfile(1001, "Mine", "", "")
    daemon.policy.users[0].media_level = "all"
    daemon.impl_SaveProfile(1001, "Mine", "", "")
    assert len(daemon.policy.custom_profiles) == 1
    assert daemon.policy.custom_profiles[0]["media_level"] == "all"


def test_a_built_in_name_cannot_be_shadowed():
    daemon = _daemon([tuned_user()])
    daemon.policy.custom_profiles = []
    # slug("child") -> "custom-child", so it can never equal "child"; but a
    # preset must still not be nameable after a built-in key outright.
    with pytest.raises(PolicyError):
        daemon.impl_SaveProfile(1001, "   ", "", "")


def test_a_saved_preset_can_be_applied_to_another_account():
    other = UserPolicy(uid=1002, username="chaim", mode="unfiltered")
    daemon = _daemon([tuned_user(), other])
    daemon.impl_SaveProfile(1001, "Mine", "", "")
    daemon.impl_ApplyProfile(1002, "custom-mine", "")
    assert other.mode == "filtered"
    assert other.blocked_categories == ["adult", "gambling", "social", "sports"]
    assert other.media_level == "immodest"
    assert other.can_install_apps is False


def test_a_preset_can_create_a_new_account_directly(monkeypatch):
    import pwd

    daemon = _daemon([tuned_user()])
    daemon.impl_SaveProfile(1001, "Mine", "", "")
    monkeypatch.setattr(pwd, "getpwnam", lambda n: (_ for _ in ()).throw(KeyError(n)))
    monkeypatch.setattr(Daemon, "_accounts_create_user", lambda self, u, f: 1003)
    monkeypatch.setattr("kosherd.daemon.subprocess.run",
                        lambda *a, **k: type("R", (), {"returncode": 0, "stderr": "", "stdout": ""})())
    daemon.impl_CreateUser("moshe", "Moshe", "custom-mine")
    created = daemon.policy.user(1003)
    assert created.mode == "filtered" and created.media_level == "immodest"


def test_deleting_removes_only_custom_presets():
    daemon = _daemon([tuned_user()])
    daemon.impl_SaveProfile(1001, "Mine", "", "")
    with pytest.raises(PolicyError):
        daemon.impl_DeleteProfile("child", "")
    daemon.impl_DeleteProfile("custom-mine", "")
    assert daemon.policy.custom_profiles == []
    with pytest.raises(PolicyError):
        daemon.impl_DeleteProfile("custom-mine", "")


def test_list_profiles_includes_the_familys_presets():
    daemon = _daemon([tuned_user()])
    daemon.impl_SaveProfile(1001, "Mine", "", "")
    listed = json.loads(daemon.impl_ListProfiles().unpack()[0])
    assert any(p["key"] == "custom-mine" and p["custom"] for p in listed)
    assert any(p["key"] == "child" and not p["custom"] for p in listed)
