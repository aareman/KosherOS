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


def test_the_guest_is_an_account_every_filter_setter_can_find():
    # The guest used to be reachable only through SetGuestConfig, so it
    # could only ever be given a preset. Now the per-account setters find
    # it by uid like anybody else, and the change lands in the guest policy.
    from kosherd.daemon import Daemon
    from kosherd.policy import GuestPolicy, Policy, PolicyError

    daemon = Daemon.__new__(Daemon)
    daemon.policy = Policy(revision=1, guest=GuestPolicy(enabled=True, uid=1010,
                                                         mode="filtered"))
    daemon._save_and_apply = lambda: None
    daemon.impl_SetBlockedCategories(1010, ["adult", "sports"], "")
    daemon.impl_SetMediaLevel(1010, "all", "")
    daemon.impl_SetLanguageFilter(1010, "block", "")
    daemon.impl_SetYouTube(1010, '{"restrict": "strict"}', "")
    daemon.impl_SetCoverStyle(1010, "skin")
    g = daemon.policy.guest
    assert g.blocked_categories == ["adult", "sports"]
    assert g.media_level == "all" and g.language_filter == "block"
    assert g.youtube == {"restrict": "strict"} and g.cover_style == "skin"
    # ...and round-trips through the schema.
    from kosherd import policy as policy_mod

    policy_mod.validate(daemon.policy.to_dict())
    again = Policy.from_dict(daemon.policy.to_dict())
    assert again.guest.cover_style == "skin"

    # Off, or a different uid: not an account.
    daemon.policy.guest.enabled = False
    import pytest

    with pytest.raises(PolicyError):
        daemon.impl_SetMediaLevel(1010, "none", "")


def test_account_management_calls_still_refuse_the_guest():
    from kosherd.daemon import Daemon
    from kosherd.policy import GuestPolicy, Policy, PolicyError

    daemon = Daemon.__new__(Daemon)
    daemon.policy = Policy(revision=1, guest=GuestPolicy(enabled=True, uid=1010))
    daemon._save_and_apply = lambda: None
    import pytest

    with pytest.raises(PolicyError):
        daemon.impl_SetUserCanInstall(1010, True)

