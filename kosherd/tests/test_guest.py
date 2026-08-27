import json
from pathlib import Path

from kosherd import dns, nft
from kosherd.policy import GUEST_USERNAME, GuestPolicy, Policy

EXAMPLE = Path(__file__).parents[2] / "policy" / "examples" / "family.json"


def example() -> Policy:
    return Policy.from_dict(json.loads(EXAMPLE.read_text()))


def test_guest_roundtrip():
    pol = example()
    assert pol.guest.enabled and pol.guest.uid == 1010 and pol.guest.mode == "whitelist"
    assert Policy.from_dict(pol.to_dict()).to_dict() == pol.to_dict()


def test_guest_in_effective_users():
    pol = example()
    guest = next(u for u in pol.effective_users() if u.username == GUEST_USERNAME)
    assert guest.uid == 1010 and guest.mode == "whitelist"
    assert "chinuch.org" in guest.whitelist
    # guest is enforcement-only, never a persisted user entry
    assert all(u.username != GUEST_USERNAME for u in pol.users)


def test_disabled_or_uncreated_guest_not_enforced():
    pol = example()
    pol.guest.enabled = False
    assert all(u.username != GUEST_USERNAME for u in pol.effective_users())
    pol.guest.enabled = True
    pol.guest.uid = None  # enabled but account not created yet
    assert all(u.username != GUEST_USERNAME for u in pol.effective_users())


def test_guest_rendered_into_nft_and_dnsmasq():
    pol = example()
    ruleset = nft.render(pol, dns_uid=989)
    assert "1010 : jump mode_whitelist" in ruleset
    assert "nftset=/chinuch.org/" in dns.render(pol)


def test_policy_without_guest_key_defaults_disabled():
    doc = example().to_dict()
    del doc["guest"]
    pol = Policy.from_dict(doc)
    assert pol.guest == GuestPolicy()
