"""Tailscale and Syncthing are off until an administrator turns them on (#220).

What is decided here: a new install has both off and its policy says so
once kosherd has started; a computer that already used either keeps it on;
off, Syncthing is offered to nobody, runs for nobody and has no ports, and
tailscaled is stopped and refuses a sign-in; the Tailscale switch puts the
service back if the policy cannot be saved.
"""

from __future__ import annotations

import base64
import json

import pytest

from kosherd import access, appaccess, nft, policy as policy_mod
from kosherd import syncthing as st
from kosherd import tailnet
from kosherd.policy import Policy, UserPolicy

REF = st.REF


def user(uid=1001, name="kid", mode="filtered", **kw) -> UserPolicy:
    return UserPolicy(uid=uid, username=name, mode=mode, **kw)


# ---- the policy --------------------------------------------------------------------

def test_undecided_switches_are_left_out_of_the_file():
    assert "advanced" not in Policy(revision=1).to_dict()


def test_the_switches_survive_a_round_trip():
    doc = Policy(revision=1, tailscale=True, syncthing=False).to_dict()
    assert doc["advanced"] == {"tailscale": True, "syncthing": False}
    policy_mod.validate(doc)
    again = Policy.from_dict(doc)
    assert (again.tailscale, again.syncthing) == (True, False)


def test_syncthing_is_switched_off_until_it_is_on():
    assert REF in Policy(revision=1).switched_off_apps()
    assert REF in Policy(revision=1, syncthing=False).switched_off_apps()
    assert not Policy(revision=1, syncthing=True).switched_off_apps()


# ---- Syncthing, off ------------------------------------------------------------------

def test_off_is_the_first_reason_even_for_an_administrator():
    admin = user(admin=True, mode="unfiltered")
    app = {"ref": REF, "categories": ["Utility"]}
    assert appaccess.decide(admin, app, {REF}, {REF}) == "off"
    assert appaccess.decide(admin, app, {REF}) is None


def test_the_store_does_not_offer_it_while_it_is_off():
    admin = user(admin=True, mode="unfiltered", app_access="store")
    index = [{"ref": REF, "name": "Syncthing", "categories": ["Utility"]},
             {"ref": "org.gnome.Chess", "name": "Chess", "categories": ["Game"]}]
    shown = {a["ref"] for a in appaccess.visible(admin, index, [], {REF})}
    assert shown == {"org.gnome.Chess"}
    assert REF in {a["ref"] for a in appaccess.visible(admin, index, [])}


def test_off_nobody_runs_it_and_no_port_is_opened():
    approved = user(approved_apps=[REF], syncthing_lan=True)
    off = Policy(revision=1, syncthing=False, users=[approved])
    assert st.run_state(off) == {"out": [], "lan": []}
    unapproved = Policy(revision=1, syncthing=True, users=[user(syncthing_lan=True)])
    inputs = {"gui": {}, "listen": {1001: 22000}}

    def render(p):
        return nft.render(p, dns_uid=988, mitm_uid=987, syncthing=inputs)

    assert render(off) == render(unapproved), "off is the same rules as nobody allowed"
    on = Policy(revision=1, syncthing=True, users=[approved])
    assert render(on) != render(off)
    assert st.run_state(on) == {"out": ["kid"], "lan": ["kid"]}


# ---- Tailscale's state file ------------------------------------------------------------

def _state(tmp_path, profiles: dict):
    path = tmp_path / "tailscaled.state"
    raw = json.dumps(profiles).encode()
    path.write_text(json.dumps({"_machinekey": "eA==",
                                "_profiles": base64.b64encode(raw).decode()}))
    return path


def test_a_signed_in_state_file_counts_as_signed_in(tmp_path):
    path = _state(tmp_path, {"abcd": {"ID": "abcd", "Name": "parent@example.com"}})
    assert tailnet.signed_in_before(path)


def test_a_signed_out_state_file_does_not(tmp_path):
    assert not tailnet.signed_in_before(_state(tmp_path, {}))


@pytest.mark.parametrize("text", [None, "", "not json", '{"_profiles": "%%%"}', "[]"])
def test_no_state_or_a_broken_one_counts_as_no(tmp_path, text):
    path = tmp_path / "tailscaled.state"
    if text is not None:
        path.write_text(text)
    assert not tailnet.signed_in_before(path)


# ---- the daemon ----------------------------------------------------------------------------

@pytest.fixture
def daemon(monkeypatch):
    from kosherd.daemon import Daemon

    d = Daemon.__new__(Daemon)
    d.policy = Policy(revision=1, users=[user()])
    d.connection = None
    d.saved = []
    d.applied = []
    d.services = []
    monkeypatch.setattr(policy_mod, "save", lambda p, *a, **k: d.saved.append(p.advanced()))
    monkeypatch.setattr(Daemon, "_save_and_apply",
                        lambda self: (self.saved.append(self.policy.advanced()),
                                      self.applied.append(True)))
    monkeypatch.setattr(tailnet, "installed", lambda: True)
    monkeypatch.setattr(tailnet, "set_running",
                        lambda on, wait=True: d.services.append((on, wait)))
    monkeypatch.setattr(tailnet, "refresh_dns_later", lambda delay=20: None)
    monkeypatch.setattr(tailnet, "forget", lambda: d.services.append("forgotten"))
    return d


def test_a_new_install_starts_with_both_off(daemon, monkeypatch):
    monkeypatch.setattr(tailnet, "signed_in_before", lambda: False)
    monkeypatch.setattr(st, "load_registry", lambda: {})
    daemon._settle_advanced()
    assert daemon.saved == [{"tailscale": False, "syncthing": False}]
    assert daemon.services == [(False, False)], "tailscaled stopped, without waiting"


def test_a_computer_already_using_them_keeps_both_on(daemon, monkeypatch):
    monkeypatch.setattr(tailnet, "signed_in_before", lambda: True)
    monkeypatch.setattr(st, "load_registry", lambda: {"kid": {"uid": 1001}})
    daemon._settle_advanced()
    assert daemon.saved == [{"tailscale": True, "syncthing": True}]
    assert daemon.services == [(True, False)]


def test_an_approval_alone_keeps_syncthing_on(daemon, monkeypatch):
    daemon.policy.users[0].approved_apps = [REF]
    monkeypatch.setattr(tailnet, "signed_in_before", lambda: False)
    monkeypatch.setattr(st, "load_registry", lambda: {})
    daemon._settle_advanced()
    assert daemon.policy.syncthing is True


def test_switches_already_decided_are_not_decided_again(daemon, monkeypatch):
    daemon.policy.tailscale, daemon.policy.syncthing = False, True
    monkeypatch.setattr(tailnet, "signed_in_before", lambda: pytest.fail("decided again"))
    daemon._settle_advanced()
    assert daemon.saved == []
    assert daemon.services == [(False, False)]


def test_syncthing_switch_is_saved_and_applied(daemon):
    daemon.impl_SetSyncthingEnabled(True, "")
    assert daemon.policy.syncthing is True and daemon.applied
    daemon.impl_SetSyncthingEnabled(False, "")
    assert daemon.policy.syncthing is False


def test_syncthing_cannot_be_allowed_for_an_account_while_it_is_off(daemon):
    from kosherd.daemon import PolicyError

    daemon.policy.syncthing = False
    with pytest.raises(PolicyError, match="switched off"):
        daemon.impl_SetUserAppApproval(1001, REF, True, "")
    daemon.policy.users[0].approved_apps = [REF]
    daemon.impl_SetUserAppApproval(1001, REF, False, "")
    assert daemon.policy.users[0].approved_apps == []


def test_tailscale_on_starts_the_service_then_saves(daemon):
    daemon.impl_SetTailscaleEnabled(True, "")
    assert daemon.services == [(True, True)]
    assert daemon.saved == [{"tailscale": True}]


def test_tailscale_off_stops_it_and_takes_the_names_away(daemon):
    daemon.policy.tailscale = True
    daemon.impl_SetTailscaleEnabled(False, "")
    assert daemon.services == [(False, True), "forgotten"]
    assert daemon.policy.tailscale is False


def test_a_failed_save_puts_tailscale_back(daemon, monkeypatch):
    def refuse(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(policy_mod, "save", refuse)
    with pytest.raises(OSError):
        daemon.impl_SetTailscaleEnabled(True, "")
    assert daemon.services == [(True, True), (False, True)]


def test_a_service_that_will_not_start_changes_nothing(daemon, monkeypatch):
    from kosherd.daemon import PolicyError

    def refuse(on, wait=True):
        raise tailnet.TailnetError("could not start Tailscale: unit failed")

    monkeypatch.setattr(tailnet, "set_running", refuse)
    with pytest.raises(PolicyError, match="could not start"):
        daemon.impl_SetTailscaleEnabled(True, "")
    assert daemon.policy.tailscale is None and daemon.saved == []


def test_off_tailscale_answers_without_asking_tailscaled(daemon, monkeypatch):
    monkeypatch.setattr(tailnet, "status", lambda: pytest.fail("tailscaled asked"))
    daemon.policy.tailscale = False
    now = json.loads(daemon.impl_TailscaleStatus().unpack()[0])
    assert now["enabled"] is False and now["state"] == "Off" and not now["signed_in"]


def test_off_tailscale_refuses_a_sign_in(daemon, monkeypatch):
    from kosherd.daemon import PolicyError

    monkeypatch.setattr(tailnet, "sign_in", lambda **_k: pytest.fail("signed in"))
    daemon.policy.tailscale = False
    with pytest.raises(PolicyError, match="switched off"):
        daemon.impl_TailscaleSignIn("")


def test_both_switches_are_gated_and_logged():
    for method in ("SetTailscaleEnabled", "SetSyncthingEnabled"):
        assert method in access.ACTIONS
        assert method in access.GUARDIAN_GATED
        assert method in access.CHANGES
        assert method not in access.PER_USER
