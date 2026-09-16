"""Groups: save an account's settings under a name, put accounts in it, and
change them all by changing the group.

The user's ask: no prebuilt presets, "but to yes allow for the concept of
groups so you can create your own ... but should be able to update one and
apply to all consumers of the preset."
"""

import json

import pytest

from kosherd import profiles
from kosherd.daemon import Daemon, PolicyError
from kosherd.policy import Policy, UserPolicy


def tuned_user(uid=1001, **more):
    return UserPolicy(uid=uid, username="rivky", mode="filtered",
                      blocked_categories=["adult", "gambling", "social", "sports"],
                      media_level="immodest", language_filter="substitute",
                      youtube={"restrict": "strict", "allowed_channels": ["@torah"]},
                      can_install_apps=False, **more)


def test_a_group_is_a_snapshot_of_the_account():
    group = profiles.from_user(tuned_user(), "Bais Yaakov girls", "For the girls")
    assert group.key == "custom-bais-yaakov-girls"
    assert group.mode == "filtered"
    assert group.blocked_categories == ("adult", "gambling", "social", "sports")
    assert group.media_level == "immodest"
    assert group.youtube == {"restrict": "strict", "allowed_channels": ["@torah"]}
    assert group.can_install_apps is False


def test_keys_are_slugs():
    assert profiles.slug("Teens") == "custom-teens"
    assert profiles.slug("  Yeshiva  Bochurim!! ") == "custom-yeshiva-bochurim"
    assert profiles.slug("???") == "custom-group"


def test_the_familys_groups_are_the_only_ones():
    custom = [profiles.to_dict(profiles.from_user(tuned_user(), "Mine"))]
    assert [p.key for p in profiles.all_profiles(custom)] == ["custom-mine"]
    assert profiles.get("custom-mine", custom).label == "Mine"
    with pytest.raises(KeyError):
        profiles.get("custom-mine")  # unknown without the family's list


def test_groups_and_membership_survive_the_policy_round_trip_and_the_schema():
    from kosherd.policy import validate

    user = tuned_user(profile="custom-mine")
    policy = Policy(revision=3, users=[user],
                    custom_profiles=[profiles.to_dict(profiles.from_user(user, "Mine", "d"))])
    doc = json.loads(json.dumps(policy.to_dict()))
    assert doc["custom_profiles"][0]["key"] == "custom-mine"
    assert doc["users"][0]["profile"] == "custom-mine"
    validate(doc)  # the shipped schema must accept it
    back = Policy.from_dict(doc)
    assert back.custom_profiles == policy.custom_profiles
    assert back.users[0].profile == "custom-mine"


def test_a_policy_without_groups_serialises_as_before():
    doc = Policy(revision=1, users=[tuned_user()]).to_dict()
    assert "custom_profiles" not in doc
    assert "profile" not in doc["users"][0]


# -- the daemon ------------------------------------------------------------------

def _daemon(users, custom=None):
    daemon = Daemon.__new__(Daemon)
    daemon.policy = Policy(revision=1, users=users,
                           custom_profiles=list(custom or []))
    daemon.connection = None
    daemon._saved = 0
    daemon._applied = 0
    daemon._save_only = lambda: setattr(daemon, "_saved", daemon._saved + 1)
    daemon._save_and_apply = lambda: setattr(daemon, "_applied", daemon._applied + 1)
    return daemon


def test_saving_makes_the_group_and_puts_the_account_in_it():
    daemon = _daemon([tuned_user()])
    key = daemon.impl_SaveProfile(1001, "Mine", "desc", "").unpack()[0]
    assert key == "custom-mine"
    assert daemon.policy.custom_profiles[0]["label"] == "Mine"
    assert daemon.policy.users[0].profile == "custom-mine"
    assert (daemon._saved, daemon._applied) == (1, 0), "nobody else's enforcement changed"


def test_a_group_needs_a_name():
    daemon = _daemon([tuned_user()])
    with pytest.raises(PolicyError):
        daemon.impl_SaveProfile(1001, "   ", "", "")


def test_putting_an_account_in_a_group_gives_it_the_settings_and_the_membership():
    other = UserPolicy(uid=1002, username="chaim", mode="unfiltered")
    daemon = _daemon([tuned_user(), other])
    daemon.impl_SaveProfile(1001, "Mine", "", "")
    daemon.impl_ApplyProfile(1002, "custom-mine", "")
    assert other.profile == "custom-mine"
    assert other.mode == "filtered"
    assert other.blocked_categories == ["adult", "gambling", "social", "sports"]
    assert other.media_level == "immodest"
    assert other.can_install_apps is False
    assert daemon._applied == 1


def test_saving_the_group_again_from_one_member_changes_every_member():
    # "should be able to update one and apply to all consumers"
    other = UserPolicy(uid=1002, username="chaim", mode="unfiltered")
    outsider = UserPolicy(uid=1003, username="dov", mode="filtered", media_level="none")
    daemon = _daemon([tuned_user(), other, outsider])
    daemon.impl_SaveProfile(1001, "Mine", "", "")
    daemon.impl_ApplyProfile(1002, "custom-mine", "")
    daemon.policy.users[0].media_level = "all"
    daemon.policy.users[0].blocked_categories.append("games")
    daemon.impl_SaveProfile(1001, "Mine", "", "")
    assert len(daemon.policy.custom_profiles) == 1
    assert daemon.policy.custom_profiles[0]["media_level"] == "all"
    assert other.media_level == "all" and "games" in other.blocked_categories
    assert outsider.media_level == "none", "an account in no group is left alone"
    assert daemon._applied == 2, "members changed, so enforcement is re-rendered"


def test_an_account_can_leave_its_group_and_keep_its_settings():
    daemon = _daemon([tuned_user()])
    daemon.impl_SaveProfile(1001, "Mine", "", "")
    daemon.impl_ApplyProfile(1001, "", "")
    user = daemon.policy.users[0]
    assert user.profile is None
    assert user.media_level == "immodest"
    assert daemon._saved == 2 and daemon._applied == 0


def test_a_member_that_drifted_is_still_a_member_and_can_be_put_back():
    daemon = _daemon([tuned_user()])
    daemon.impl_SaveProfile(1001, "Mine", "", "")
    user = daemon.policy.users[0]
    user.media_level = "none"
    key, changes = profiles.diff(user, daemon.policy.custom_profiles)
    assert key == "custom-mine" and [c["field"] for c in changes] == ["media_level"]
    daemon.impl_ApplyProfile(1001, "custom-mine", "")
    assert user.media_level == "immodest"
    assert profiles.diff(user, daemon.policy.custom_profiles) == ("custom-mine", [])


def test_a_group_can_create_a_new_account_directly(monkeypatch):
    import pwd

    daemon = _daemon([tuned_user()])
    daemon.impl_SaveProfile(1001, "Mine", "", "")
    monkeypatch.setattr(pwd, "getpwnam", lambda n: (_ for _ in ()).throw(KeyError(n)))
    monkeypatch.setattr(Daemon, "_accounts_create_user", lambda self, u, f: 1003)
    monkeypatch.setattr("kosherd.daemon.subprocess.run",
                        lambda *a, **k: type("R", (), {"returncode": 0, "stderr": "", "stdout": ""})())
    daemon.impl_CreateUser("moshe", "Moshe", "custom-mine")
    created = daemon.policy.user(1003)
    assert created.profile == "custom-mine"
    assert created.mode == "filtered" and created.media_level == "immodest"


def test_deleting_a_group_leaves_its_members_with_their_settings_and_no_group():
    other = UserPolicy(uid=1002, username="chaim", mode="unfiltered")
    daemon = _daemon([tuned_user(), other])
    daemon.impl_SaveProfile(1001, "Mine", "", "")
    daemon.impl_ApplyProfile(1002, "custom-mine", "")
    daemon.impl_DeleteProfile("custom-mine", "")
    assert daemon.policy.custom_profiles == []
    assert other.profile is None and other.mode == "filtered"
    with pytest.raises(PolicyError):
        daemon.impl_DeleteProfile("custom-mine", "")


def test_list_profiles_is_the_familys_groups_and_nothing_else():
    daemon = _daemon([tuned_user()])
    assert json.loads(daemon.impl_ListProfiles().unpack()[0]) == []
    daemon.impl_SaveProfile(1001, "Mine", "", "")
    listed = json.loads(daemon.impl_ListProfiles().unpack()[0])
    assert [p["key"] for p in listed] == ["custom-mine"]


def test_the_guest_set_up_by_kind_gets_that_kinds_complete_settings(monkeypatch):
    daemon = _daemon([tuned_user()])
    monkeypatch.setattr("kosherd.daemon.subprocess.run",
                        lambda *a, **k: type("R", (), {"returncode": 0, "stderr": "", "stdout": ""})())
    daemon.impl_SetGuestConfig(False, "whitelist", ["chabad.org"], "")
    g = daemon.policy.guest
    assert g.mode == "whitelist" and g.media_level == "all"
    assert g.language_filter == "substitute"


# -- the command line -------------------------------------------------------------

def test_the_cli_speaks_groups(monkeypatch, capsys):
    from kosherd import cli

    calls = []

    class C:
        def delete_profile(self, key, pw=""):
            calls.append(("delete", key))

        def save_profile(self, uid, label, desc, pw=""):
            calls.append(("save", uid, label)); return "custom-x"

        def apply_profile(self, uid, key, pw=""):
            calls.append(("set", uid, key))

        def list_profiles(self):
            return []

    monkeypatch.setattr(cli, "_client", lambda: C())
    monkeypatch.setattr(cli, "_guardian_pw", lambda a: "")
    assert cli.main(["group", "delete", "custom-x"]) == 0
    assert cli.main(["group", "save", "1000", "Mine"]) == 0
    assert cli.main(["group", "set", "1000", "custom-x"]) == 0
    assert cli.main(["group", "set", "1000", "none"]) == 0
    assert cli.main(["profile", "set", "1000", "custom-x"]) == 0, "the old name still works"
    assert calls == [("delete", "custom-x"), ("save", 1000, "Mine"), ("set", 1000, "custom-x"),
                     ("set", 1000, ""), ("set", 1000, "custom-x")]
    assert cli.main(["group", "set", "notanumber", "custom-x"]) == 2
    assert cli.main(["group", "list"]) == 0
    assert "no groups yet" in capsys.readouterr().out
