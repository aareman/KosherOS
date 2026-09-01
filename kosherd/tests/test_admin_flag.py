"""Making somebody an administrator, and not locking everybody out.

There was no way to do this at all: the first administrator is created
during setup and a second parent could never be made one afterwards,
which is a strange hole in a product whose model is two parents sharing
control.
"""

import pytest

from kosherd.daemon import Daemon, PolicyError
from kosherd.policy import Policy, UserPolicy


def _daemon(users, monkeypatch, usermod_ok=True):
    calls = []

    def run(argv, **kw):
        calls.append(argv)
        return type("R", (), {"returncode": 0 if usermod_ok else 1,
                              "stderr": "" if usermod_ok else "no such group",
                              "stdout": ""})()

    monkeypatch.setattr("kosherd.daemon.subprocess.run", run)
    daemon = Daemon.__new__(Daemon)
    daemon.policy = Policy(revision=1, users=users)
    daemon._save_and_apply = lambda: None
    return daemon, calls


def test_a_second_parent_can_be_made_an_administrator(monkeypatch):
    parent = UserPolicy(uid=1000, username="abba", mode="unfiltered", admin=True)
    other = UserPolicy(uid=1001, username="ima", mode="unfiltered")
    daemon, calls = _daemon([parent, other], monkeypatch)
    daemon.impl_SetUserAdmin(1001, True, "")
    assert other.admin is True
    assert ["usermod", "-aG", "kosher-admin", "ima"] in calls


def test_an_administrator_can_be_demoted_when_another_remains(monkeypatch):
    a = UserPolicy(uid=1000, username="abba", mode="unfiltered", admin=True)
    b = UserPolicy(uid=1001, username="ima", mode="unfiltered", admin=True)
    daemon, calls = _daemon([a, b], monkeypatch)
    daemon.impl_SetUserAdmin(1001, False, "")
    assert b.admin is False
    assert ["usermod", "-rG", "kosher-admin", "ima"] in calls


def test_the_last_administrator_cannot_demote_themselves(monkeypatch):
    # Nobody would be left who could undo it, including them. The machine
    # would need reinstalling.
    only = UserPolicy(uid=1000, username="abba", mode="unfiltered", admin=True)
    daemon, _calls = _daemon([only, UserPolicy(uid=1001, username="kid",
                                               mode="filtered")], monkeypatch)
    with pytest.raises(PolicyError) as caught:
        daemon.impl_SetUserAdmin(1000, False, "")
    assert "only administrator" in str(caught.value)
    assert only.admin is True


def test_the_last_administrator_cannot_be_removed(monkeypatch):
    only = UserPolicy(uid=1000, username="abba", mode="unfiltered", admin=True)
    daemon, _calls = _daemon([only], monkeypatch)
    with pytest.raises(PolicyError) as caught:
        daemon.impl_RemoveUser(1000)
    assert "nobody who could change anything" in str(caught.value)
    assert only in daemon.policy.users


def test_a_non_administrator_can_be_removed(monkeypatch):
    admin = UserPolicy(uid=1000, username="abba", mode="unfiltered", admin=True)
    kid = UserPolicy(uid=1001, username="kid", mode="filtered")
    daemon, _calls = _daemon([admin, kid], monkeypatch)
    daemon._accounts_delete_user = lambda uid: None
    daemon.impl_RemoveUser(1001)
    assert kid not in daemon.policy.users


def test_a_failed_group_change_is_reported_not_swallowed(monkeypatch):
    # The policy would say administrator and the system would not, which
    # is the worst of both: the app shows a right that does not exist.
    a = UserPolicy(uid=1000, username="abba", mode="unfiltered", admin=True)
    b = UserPolicy(uid=1001, username="ima", mode="unfiltered")
    daemon, _calls = _daemon([a, b], monkeypatch, usermod_ok=False)
    with pytest.raises(PolicyError) as caught:
        daemon.impl_SetUserAdmin(1001, True, "")
    assert "group membership" in str(caught.value)


def test_an_unmanaged_account_cannot_be_promoted(monkeypatch):
    daemon, _calls = _daemon([], monkeypatch)
    with pytest.raises(PolicyError):
        daemon.impl_SetUserAdmin(4242, True, "")
