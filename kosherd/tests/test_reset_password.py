"""Letting a supervised account choose a new password at its next sign-in.

A child who forgets their password is otherwise locked out of that account
for good: nobody on a KosherOS machine is root, and the stock
accountsservice actions that would reset one are granted to nobody.
"""

from __future__ import annotations

import pytest

from kosherd import access
from kosherd.daemon import Daemon, PolicyError
from kosherd.policy import Policy, UserPolicy


class _Connection:
    """Enough of a D-Bus connection to see what the daemon asked for."""

    def __init__(self):
        self.calls: list[tuple] = []

    def call_sync(self, name, path, iface, method, params, reply, flags, timeout, cancel):
        self.calls.append((method, path, params.unpack() if params else None))
        if method == "FindUserById":
            from gi.repository import GLib

            return GLib.Variant("(o)", ("/org/freedesktop/Accounts/User1001",))
        return None


def _daemon(users):
    daemon = Daemon.__new__(Daemon)
    daemon.policy = Policy(revision=1, users=users)
    daemon.connection = _Connection()
    return daemon


def _child(uid=1001, **kw):
    return UserPolicy(uid=uid, username="yosef", mode="filtered", **kw)


def test_a_supervised_account_is_asked_for_a_new_password_at_sign_in():
    daemon = _daemon([_child()])
    assert daemon.impl_ResetPassword(1001) is None
    methods = [c[0] for c in daemon.connection.calls]
    assert methods == ["FindUserById", "SetPasswordMode"]
    path, params = daemon.connection.calls[1][1], daemon.connection.calls[1][2]
    assert path == "/org/freedesktop/Accounts/User1001"
    assert params == (1,), "1 is accountsservice's SET_AT_LOGIN"


def test_nobody_is_handed_a_password():
    # The method takes a uid and nothing else: there is no password to
    # type, to read over a shoulder, or to keep.
    import inspect

    signature = inspect.signature(Daemon.impl_ResetPassword)
    assert list(signature.parameters) == ["self", "uid"]


def test_an_administrators_password_cannot_be_reset_from_here():
    # It would be the way to take over the other parent's account.
    daemon = _daemon([_child(admin=True)])
    with pytest.raises(PolicyError) as raised:
        daemon.impl_ResetPassword(1001)
    assert "administrator" in str(raised.value)
    assert daemon.connection.calls == [], "and accountsservice is never asked"


def test_an_account_this_machine_does_not_manage_is_refused():
    daemon = _daemon([_child()])
    with pytest.raises(PolicyError):
        daemon.impl_ResetPassword(4242)


def test_it_is_account_management_and_needs_no_guardian_password():
    # A parent should not need the other parent present to let a child back
    # into their own account, and this weakens no filter.
    assert access.ACTIONS["ResetPassword"] == access.ACTION_MANAGE_USERS
    assert "ResetPassword" not in access.GUARDIAN_GATED


def test_it_is_written_down_as_a_change_to_that_account():
    # "who let this account back in, and when" is a question a household
    # with two administrators will ask.
    assert "ResetPassword" in access.CHANGES
    assert "ResetPassword" in access.PER_USER


def test_the_activity_log_has_words_for_it():
    # The admin app needs GTK to import, which this environment has not, so
    # its source is read — the same way test_admin_app_labels.py does it.
    from pathlib import Path

    labels = (Path(__file__).parents[2] / "admin-app/src/kosheradmin/labels.py").read_text()
    assert 'elif method == "ResetPassword":' in labels
    assert "password reset" in labels and "next sign-in" in labels
