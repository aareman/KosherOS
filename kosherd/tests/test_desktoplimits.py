"""F9: account-specific desktop controls, without claiming an exec sandbox."""

import shutil
import subprocess

import pytest

from kosherd import desktoplimits
from kosherd.mct import filter_spec
from kosherd.policy import Policy, UserPolicy


def user(**fields):
    return UserPolicy(**{"uid": 1001, "username": "kid", "mode": "filtered", **fields})


@pytest.mark.parametrize("fields, expected", [
    ({}, True),
    ({"admin": True}, False),
    ({"developer_tools": True}, False),
    ({"app_access": "store"}, False),
    ({"app_access": "store", "blocked_apps": ["org.example.App"]}, True),
    ({"app_access": "store", "blocked_app_kinds": ["games"]}, True),
])
def test_desktop_limits_follow_the_account(fields, expected):
    assert desktoplimits.restricted(user(**fields)) is expected


def test_restricted_accounts_hide_system_terminals_but_developers_keep_them():
    assert set(desktoplimits.TERMINALS) <= set(filter_spec(user(), []).blocked_paths)
    for fields in ({"admin": True}, {"developer_tools": True}):
        assert not set(desktoplimits.TERMINALS) & set(filter_spec(user(**fields), []).blocked_paths)


def test_profile_selection_does_not_touch_the_greeter_or_unmanaged_accounts(tmp_path):
    (tmp_path / "kosher-1001").write_text("user-db:user\n")
    assert desktoplimits.selected_profile(1001, tmp_path) == tmp_path / "kosher-1001"
    assert desktoplimits.selected_profile(1002, tmp_path) is None
    assert desktoplimits.selected_profile(42, tmp_path) is None
    assert desktoplimits.selected_profile(0, tmp_path) is None


@pytest.mark.skipif(shutil.which("dconf") is None, reason="dconf compiler unavailable")
def test_real_databases_compile_per_account_and_unlock_when_developer_tools_are_enabled(tmp_path):
    profiles, databases = tmp_path / "profiles", tmp_path / "db"
    kid = user()
    admin = user(uid=1002, username="parent", admin=True)
    policy = Policy(users=[kid, admin])
    calls = []

    def run(argv, **kw):
        calls.append(argv)
        return subprocess.run(argv, **kw)

    desktoplimits.apply(policy, profile_dir=profiles, database_dir=databases, run=run)
    for uid in (1001, 1002):
        assert (databases / f"kosher-{uid}").stat().st_size > 0
        assert (profiles / f"kosher-{uid}").read_text() == (
            f"user-db:user\nsystem-db:kosher-{uid}\nsystem-db:local\n")
    kid_locks = databases / "kosher-1001.d/locks/00-policy"
    assert "disable-command-line" in kid_locks.read_text()
    assert "disable-user-switching" in kid_locks.read_text()
    assert (databases / "kosher-1002.d/locks/00-policy").read_text() == ""
    desktoplimits.apply(policy, profile_dir=profiles, database_dir=databases, run=run)
    assert len(calls) == 1, "unrelated policy changes should not recompile desktop settings"
    kid.developer_tools = True
    desktoplimits.apply(policy, profile_dir=profiles, database_dir=databases, run=run)
    assert len(calls) == 2
    assert kid_locks.read_text() == ""
    assert "disable-command-line=false" in (databases / "kosher-1001.d/00-policy").read_text()


def test_failed_compilation_does_not_publish_a_broken_profile(tmp_path):
    def fail(argv, **kw):
        raise subprocess.CalledProcessError(1, argv)

    with pytest.raises(subprocess.CalledProcessError):
        desktoplimits.apply(Policy(users=[user()]), profile_dir=tmp_path / "profiles",
                            database_dir=tmp_path / "db", run=fail)
    assert not (tmp_path / "profiles/kosher-1001").exists()
