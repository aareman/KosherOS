"""Creating a user is all or nothing.

The first family test typed "Elisha": accountsservice made the account,
the policy then refused the name, and the machine was left with a user
nothing managed AND an in-memory policy that carried the refused user, so
every later call failed the same way until the daemon restarted. Now the
name is checked before anything is created, a save that fails removes the
account it just made, and the dispatcher puts the policy back after any
failed call.
"""

import pytest

from kosherd.daemon import Daemon
from kosherd.policy import Policy, PolicyError, UserPolicy


def _daemon(users=()):
    daemon = Daemon.__new__(Daemon)
    daemon.policy = Policy(revision=1, users=list(users))
    daemon.connection = None
    return daemon


def test_a_bad_username_is_refused_before_any_account_is_made(monkeypatch):
    daemon = _daemon()
    created = []
    monkeypatch.setattr(daemon, "_accounts_create_user",
                        lambda u, f: created.append(u) or 1001)
    with pytest.raises(PolicyError) as raised:
        daemon.impl_CreateUser("eli sha", "Eli", "child")
    assert "not a valid username" in str(raised.value)
    assert created == []
    assert daemon.policy.users == []


def test_capitals_are_fine(monkeypatch):
    daemon = _daemon()
    monkeypatch.setattr(daemon, "_accounts_create_user", lambda u, f: 1001)
    monkeypatch.setattr(daemon, "_save_and_apply", lambda: None)
    monkeypatch.setattr("pwd.getpwnam", lambda name: (_ for _ in ()).throw(KeyError(name)))
    daemon.impl_CreateUser("Elisha", "Elisha", "child")
    assert daemon.policy.users[0].username == "Elisha"


def test_a_save_that_fails_removes_the_account_it_just_made(monkeypatch):
    daemon = _daemon()
    deleted = []
    monkeypatch.setattr(daemon, "_accounts_create_user", lambda u, f: 1001)
    monkeypatch.setattr(daemon, "_accounts_delete_user", lambda uid: deleted.append(uid))
    monkeypatch.setattr(daemon, "_save_and_apply",
                        lambda: (_ for _ in ()).throw(PolicyError("schema said no")))
    monkeypatch.setattr("pwd.getpwnam", lambda name: (_ for _ in ()).throw(KeyError(name)))
    with pytest.raises(PolicyError):
        daemon.impl_CreateUser("elisha", "Elisha", "child")
    assert deleted == [1001]


def test_the_dispatcher_puts_the_policy_back_after_a_failed_call():
    import copy

    daemon = _daemon([UserPolicy(uid=1000, username="abba", mode="filtered")])
    before = copy.deepcopy(daemon.policy)
    # What a failing method leaves behind: an appended user that the save
    # refused.
    daemon.policy.users.append(UserPolicy(uid=1001, username="x", mode="filtered"))
    daemon._roll_back(before, "CreateUser")
    assert [u.username for u in daemon.policy.users] == ["abba"]


def test_a_successful_call_is_left_alone():
    daemon = _daemon()
    snapshot = None
    daemon._roll_back(snapshot, "GetPolicy")   # nothing to restore
    assert daemon.policy.users == []
