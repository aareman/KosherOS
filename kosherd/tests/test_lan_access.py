"""The LAN exception is a separate, guardian-controlled account setting."""

from types import SimpleNamespace

import pytest

from kosherd import access, cli
from kosherd.client import DaemonClient
from kosherd.daemon import Daemon, PolicyError
from kosherd.policy import Policy, UserPolicy


def daemon():
    instance = Daemon.__new__(Daemon)
    instance.policy = Policy(users=[UserPolicy(uid=1001, username="kid", mode="filtered")])
    applied = []
    instance._save_and_apply = lambda: applied.append(True)
    return instance, applied


def test_lan_permission_is_a_guardian_gated_logged_filter_change():
    assert access.ACTIONS["SetLanHosts"] == access.ACTION_MANAGE_FILTER
    assert "SetLanHosts" in access.GUARDIAN_GATED
    assert "SetLanHosts" in access.CHANGES
    assert "SetLanHosts" in access.PER_USER


def test_setter_normalizes_then_applies_for_only_the_requested_account():
    instance, applied = daemon()
    instance.impl_SetLanHosts(1001, ["Printer.Home.Arpa", "192.168.1.20", "printer.home.arpa"], "")
    assert set(instance.policy.user(1001).lan_hosts) == {"printer.home.arpa", "192.168.1.20"}
    assert applied == [True]


@pytest.mark.parametrize("hosts", [["*"], ["*.example.com"], ["https://printer.home.arpa"], ["1.1.1.1"]])
def test_invalid_exception_does_not_change_policy(hosts):
    instance, applied = daemon()
    instance.policy.user(1001).lan_hosts = ["printer.home.arpa"]
    with pytest.raises(PolicyError):
        instance.impl_SetLanHosts(1001, hosts, "")
    assert instance.policy.user(1001).lan_hosts == ["printer.home.arpa"]
    assert not applied


def test_lan_setter_refuses_unmanaged_uid():
    instance, applied = daemon()
    with pytest.raises(PolicyError, match="not managed"):
        instance.impl_SetLanHosts(9999, ["printer.home.arpa"], "")
    assert not applied


def test_client_preserves_the_guardian_argument():
    client = DaemonClient.__new__(DaemonClient)
    calls = []
    client._call = lambda *args: calls.append(args)
    client.set_lan_hosts(1001, ["printer.home.arpa"], "proof")
    assert calls == [("Profiles", "SetLanHosts", "(iass)", 1001, ["printer.home.arpa"], "proof")]


def test_cli_can_clear_the_exception(monkeypatch):
    calls = []
    monkeypatch.setattr(cli, "_client", lambda: SimpleNamespace(
        set_lan_hosts=lambda *args: calls.append(args)))
    assert cli.main(["lan-sites", "1001", "none", "--guardian-password", "proof"]) == 0
    assert calls == [(1001, [], "proof")]
