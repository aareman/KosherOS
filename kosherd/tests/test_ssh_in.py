"""Signing in to an account over SSH from another machine.

A switch under developer tools, off by default. On, kosherd writes sshd's
drop-in naming the accounts that may sign in — keys only, forwarding toward
this computer only — and sshd's unit starts because the file exists. Off for
the last account, the file goes and sshd stops with it (sshin.py).
"""

import subprocess

import pytest

from kosherd import access, apply, sshin
from kosherd.daemon import Daemon
from kosherd.policy import Policy, PolicyError, UserPolicy


def _policy(*users: UserPolicy) -> Policy:
    return Policy(revision=3, users=list(users))


def _dev(uid=1002, name="dev", mode="filtered", **kw) -> UserPolicy:
    return UserPolicy(uid=uid, username=name, mode=mode, developer_tools=True, **kw)


class _Run:
    def __init__(self):
        self.calls = []

    def __call__(self, argv, **kw):
        self.calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")


def _conf(text: str) -> dict[str, str]:
    return dict(l.split(" ", 1) for l in text.splitlines() if l and not l.startswith("#"))


# ---- the policy ---------------------------------------------------------------

def test_it_is_off_by_default_and_round_trips():
    assert UserPolicy(uid=1001, username="kid", mode="filtered").ssh_in is False
    dev = _dev(ssh_in=True)
    assert dev.to_dict()["ssh_in"] is True
    assert "ssh_in" not in _dev().to_dict()
    assert Policy.from_dict(_policy(dev).to_dict()).user(1002).ssh_in is True


def test_only_a_developer_account_with_internet_may_sign_in():
    policy = _policy(
        UserPolicy(uid=1001, username="kid", mode="filtered", ssh_in=True),
        _dev(ssh_in=True),
        _dev(uid=1003, name="quiet"),
        _dev(uid=1004, name="offline", mode="none", ssh_in=True),
    )
    assert sshin.allowed_users(policy) == ["dev"]


# ---- what sshd is told -----------------------------------------------------------

def test_nobody_allowed_means_no_file():
    assert sshin.render(_policy(_dev())) is None


def test_keys_only_and_forwarding_toward_this_computer_only():
    conf = _conf(sshin.render(_policy(_dev(ssh_in=True), _dev(uid=1005, name="b", ssh_in=True))))
    assert conf["AllowUsers"] == "root dev b"
    assert conf["PasswordAuthentication"] == "no"
    assert conf["KbdInteractiveAuthentication"] == "no"
    assert conf["PermitRootLogin"] == "prohibit-password"
    # -L connects as the account, through its filter; -R and tunnels would
    # be a listener here that reaches out unfiltered.
    assert conf["AllowTcpForwarding"] == "local"
    assert conf["AllowStreamLocalForwarding"] == "local"
    assert conf["PermitTunnel"] == "no"
    assert conf["GatewayPorts"] == "no"


def test_it_sorts_ahead_of_fedoras_own_drop_in():
    # sshd keeps the first value it reads; 50-redhat.conf must not win.
    assert sshin.CONF_PATH.parent.as_posix() == "/etc/ssh/sshd_config.d"
    assert sshin.CONF_PATH.name < "50-redhat.conf"


# ---- apply -------------------------------------------------------------------------

@pytest.fixture
def conf_path(tmp_path, monkeypatch):
    path = tmp_path / "sshd_config.d" / "40-kosheros.conf"
    path.parent.mkdir()
    monkeypatch.setattr(sshin, "CONF_PATH", path)
    return path


def test_apply_writes_starts_and_stops_only_on_change(conf_path):
    run = _Run()
    on = _policy(_dev(ssh_in=True))
    assert apply.write_ssh_in(on, run=run) is True
    assert "AllowUsers root dev\n" in conf_path.read_text()
    assert conf_path.stat().st_mode & 0o077 == 0
    assert run.calls == [["systemctl", "restart", "sshd.service"]]

    assert apply.write_ssh_in(on, run=run) is False
    assert len(run.calls) == 1

    assert apply.write_ssh_in(_policy(_dev()), run=run) is True
    assert not conf_path.exists()
    assert len(run.calls) == 2


def test_apply_leaves_a_machine_without_sshd_alone(tmp_path, monkeypatch):
    run = _Run()
    monkeypatch.setattr(sshin, "CONF_PATH", tmp_path / "none" / "40-kosheros.conf")
    assert apply.write_ssh_in(_policy(_dev(ssh_in=True)), run=run) is False
    assert run.calls == []


def test_nobody_allowed_on_a_machine_never_switched_on_does_nothing(conf_path):
    run = _Run()
    assert apply.write_ssh_in(_policy(_dev()), run=run) is False
    assert run.calls == []


# ---- the daemon ----------------------------------------------------------------

def test_the_switch_is_a_filter_change():
    assert access.ACTIONS["SetSshIn"] == access.ACTION_MANAGE_FILTER
    assert "SetSshIn" in access.GUARDIAN_GATED
    assert "SetSshIn" in access.CHANGES
    assert "SetSshIn" in access.PER_USER


def _daemon(monkeypatch, *users):
    daemon = Daemon.__new__(Daemon)
    daemon.policy = _policy(*users)
    applied = []
    monkeypatch.setattr(Daemon, "_save_and_apply", lambda self: applied.append(True))
    return daemon, applied


def test_it_needs_developer_tools(monkeypatch):
    daemon, applied = _daemon(monkeypatch, UserPolicy(uid=1001, username="kid", mode="filtered"))
    with pytest.raises(PolicyError):
        daemon.impl_SetSshIn(1001, True, "")
    assert not daemon.policy.user(1001).ssh_in and not applied


def test_setting_it_records_and_applies(monkeypatch):
    daemon, applied = _daemon(monkeypatch, _dev())
    daemon.impl_SetSshIn(1002, True, "")
    assert daemon.policy.user(1002).ssh_in is True and applied


def test_turning_developer_tools_off_takes_it_away(monkeypatch):
    daemon, _ = _daemon(monkeypatch, _dev(ssh_in=True))
    daemon.impl_SetDeveloperTools(1002, False, "")
    daemon.impl_SetDeveloperTools(1002, True, "")
    assert daemon.policy.user(1002).ssh_in is False
