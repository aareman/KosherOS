"""Store actions and whose account they land in (issue #33, October: N4, N5).

Two assertions about today's code that are known to be wrong, pinned as
strict expected failures so the suite stays green and the fix flips them:

- an administrator installing "for" an account must meet that account's
  own limits (can_install_apps), and the activity log must name the
  account the install was for;
- an update started by one account must act on that account's own
  profile, never fall back to whoever the ledger says asked first.
"""

import pytest

from kosherd import access, apps
from kosherd.daemon import Daemon, PolicyError
from kosherd.policy import Policy, UserPolicy


def _daemon(monkeypatch, **user):
    daemon = Daemon.__new__(Daemon)
    fields = dict(uid=1001, username="kid", mode="filtered")
    fields.update(user)
    daemon.policy = Policy(revision=1, users=[UserPolicy(**fields)])
    installed = []
    monkeypatch.setattr(Daemon, "_install_for",
                        lambda self, account, uid, ref: installed.append((uid, ref)))
    return daemon, installed


@pytest.mark.xfail(strict=True, reason="N5: InstallAppFor skips can_install_apps (#33)")
def test_installing_for_an_account_meets_that_accounts_own_limits(monkeypatch):
    daemon, installed = _daemon(monkeypatch, can_install_apps=False)
    with pytest.raises(PolicyError):
        daemon.impl_InstallAppFor(1001, "org.example.App")
    assert installed == []


@pytest.mark.xfail(strict=True, reason="N5: InstallAppFor is not PER_USER, so the log names no account (#33)")
def test_installing_for_an_account_is_logged_against_that_account():
    assert "InstallAppFor" in access.PER_USER


@pytest.mark.xfail(strict=True, reason="N4: _nix_owner falls back to the ledger's owner, crossing accounts (#33)")
def test_an_update_never_acts_on_another_accounts_profile(monkeypatch):
    daemon, _ = _daemon(monkeypatch)
    monkeypatch.setattr(apps.nixapps, "installed", lambda uid: [])
    monkeypatch.setattr(apps, "_load_ledger",
                        lambda: {"nixpkgs#hello": {"uid": 1002}})
    # The caller (1001) does not have the app; the ledger says 1002 asked
    # for it. The answer must not be 1002.
    with pytest.raises(PolicyError):
        daemon._nix_owner("nixpkgs#hello", 1001)
