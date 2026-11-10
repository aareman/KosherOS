"""F14: a Wi-Fi sign-in window is bounded web access, not a tunnel."""

from types import SimpleNamespace

import pytest

from kosherd import nft
from kosherd.daemon import Daemon, PolicyError
from kosherd.policy import Policy, UserPolicy


def _daemon():
    daemon = Daemon.__new__(Daemon)
    daemon.policy = Policy(users=[UserPolicy(uid=1001, username="kid", mode="none")])
    return daemon


@pytest.mark.parametrize("minutes", [-1, 0, 11, 60])
def test_window_cannot_exceed_ten_minutes(monkeypatch, minutes):
    monkeypatch.setattr("kosherd.daemon.subprocess.run",
                        lambda *a, **k: pytest.fail("invalid request reached nft"))
    with pytest.raises(PolicyError, match="1-10"):
        _daemon().impl_SetCaptiveMode(1001, minutes)


@pytest.mark.parametrize("uid", [0, -1, 9999])
def test_window_requires_a_managed_account(monkeypatch, uid):
    monkeypatch.setattr("kosherd.daemon.subprocess.run",
                        lambda *a, **k: pytest.fail("unknown account reached nft"))
    with pytest.raises(PolicyError, match="not managed"):
        _daemon().impl_SetCaptiveMode(uid, 5)


def test_ten_minute_window_names_only_the_requested_account(monkeypatch):
    calls = []
    monkeypatch.setattr("kosherd.daemon.subprocess.run", lambda argv, **kw:
                        calls.append((argv, kw["input"])) or SimpleNamespace(returncode=0))
    _daemon().impl_SetCaptiveMode(1001, 10)
    assert calls == [(["nft", "-f", "-"],
                      "add element inet kosher captive { 1001 timeout 10m }\n"
                      "add element inet kosher captive_marks { 1001 timeout 10m }\n")]


def test_captive_web_bypasses_proxy_but_dns_does_not():
    policy = Policy(users=[UserPolicy(uid=1001, username="kid", mode="filtered")])
    rules = nft.render(policy, dns_uid=989, mitm_uid=988)
    dns = rules.index("tcp dport 53 redirect to :53")
    portal = rules.index("meta skuid @captive tcp dport { 80, 443 } return")
    proxy = rules.index("redirect to :30000")
    assert dns < portal < proxy
    assert "meta skuid @captive accept" not in rules


def test_expired_captive_sessions_cannot_use_the_established_shortcut():
    rules = nft.render(_daemon().policy, dns_uid=989, mitm_uid=988)
    expired = rules.index(f"ct label {nft.CAPTIVE_LABEL} ct mark != @captive_marks reject")
    web = rules.index("meta skuid @captive tcp dport { 80, 443 } ct mark set")
    established = rules.index("ct state established,related accept")
    assert expired < web < established
    incoming = rules.split("chain input {")[1]
    assert incoming.index(f"ct label {nft.CAPTIVE_LABEL} ct mark != @captive_marks drop") \
        < incoming.index("ct state established,related accept")
