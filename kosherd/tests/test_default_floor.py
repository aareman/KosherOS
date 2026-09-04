"""Every account in a filtering mode blocks SOMETHING by default.

The first hands-on family test found the administrator — the account every
machine has, created by the setup wizard — with no category list at all:
gambling, dating and VPN sites all loaded, while "filtered" showed in the
admin app. CreateUser applied the default floor; CreateFirstAdmin built the
account by hand and skipped it. These pin the floor on every path into a
filtering mode.
"""

import pwd

import pytest

from kosherd.categories import DEFAULT_BLOCKED
from kosherd.daemon import Daemon
from kosherd.policy import Policy, UserPolicy


def _daemon(users, monkeypatch):
    def run(argv, **kw):
        return type("R", (), {"returncode": 0, "stderr": "", "stdout": ""})()

    monkeypatch.setattr("kosherd.daemon.subprocess.run", run)
    daemon = Daemon.__new__(Daemon)
    daemon.policy = Policy(revision=1, users=users)
    daemon._save_and_apply = lambda: None
    return daemon


def test_the_first_admin_gets_the_default_floor(monkeypatch):
    daemon = _daemon([], monkeypatch)
    monkeypatch.setattr(pwd, "getpwnam", lambda name: (_ for _ in ()).throw(KeyError(name)))
    monkeypatch.setattr(Daemon, "_accounts_create_user", lambda self, u, f: 1000)
    daemon.impl_CreateFirstAdmin("abba", "Abba", "kosher-1")
    admin = daemon.policy.user(1000)
    assert admin.admin and admin.mode == "filtered"
    assert set(admin.blocked_categories) == set(DEFAULT_BLOCKED), \
        "the administrator must not be the one account that filters nothing"
    for must in ("adult", "gambling", "dating", "proxy"):
        assert must in admin.blocked_categories


def test_switching_into_a_filtering_mode_applies_the_floor(monkeypatch):
    user = UserPolicy(uid=1001, username="ima", mode="unfiltered")
    daemon = _daemon([user], monkeypatch)
    assert user.blocked_categories == []
    daemon.impl_SetFilterMode(1001, "filtered", "")
    assert set(user.blocked_categories) == set(DEFAULT_BLOCKED)


def test_switching_modes_keeps_a_deliberate_choice(monkeypatch):
    # An admin who chose exactly {"social"} keeps it; the floor is a default
    # for an empty list, not an override.
    user = UserPolicy(uid=1001, username="ima", mode="dnsfilter",
                      blocked_categories=["social"])
    daemon = _daemon([user], monkeypatch)
    daemon.impl_SetFilterMode(1001, "filtered", "")
    assert user.blocked_categories == ["social"]


@pytest.mark.parametrize("mode", ["filtered", "dnsfilter", "whitelist"])
def test_every_filtering_mode_has_a_floor(mode):
    from kosherd.daemon import _default_categories

    assert set(_default_categories(mode)) >= {"adult", "gambling"}


def test_unfiltered_has_no_floor():
    from kosherd.daemon import _default_categories

    assert _default_categories("unfiltered") == ()


def test_an_account_that_filters_nothing_is_reported():
    from kosherd import selfcheck

    users = [UserPolicy(uid=1000, username="abba", mode="filtered"),
             UserPolicy(uid=1001, username="ima", mode="filtered",
                        blocked_categories=["adult"]),
             UserPolicy(uid=1002, username="guest", mode="unfiltered")]
    said = selfcheck.empty_accounts(users)
    assert len(said) == 1 and said[0].startswith("abba is set to filter")
