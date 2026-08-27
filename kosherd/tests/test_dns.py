import json
from pathlib import Path

from kosherd import dns
from kosherd.policy import Policy

EXAMPLE = Path(__file__).parents[2] / "policy" / "examples" / "family.json"


def rendered() -> str:
    return dns.render(Policy.from_dict(json.loads(EXAMPLE.read_text())))


def test_whitelist_domains_feed_wl_sets():
    out = rendered()
    assert "nftset=/chinuch.org/4#inet#kosher#wl4,6#inet#kosher#wl6" in out
    # wildcard prefix is stripped — dnsmasq patterns already match subdomains
    assert "nftset=/torahanytime.com/4#inet#kosher#wl4,6#inet#kosher#wl6" in out
    assert "*." not in out


def test_system_domains_feed_sys_sets():
    out = rendered()
    assert "nftset=/nmcheck.gnome.org/4#inet#kosher#sys4,6#inet#kosher#sys6" in out
    assert "nftset=/portal.example.org/4#inet#kosher#sys4,6#inet#kosher#sys6" in out


def test_non_whitelist_users_contribute_nothing():
    out = rendered()
    # dnsfilter/none users have no whitelist entries to leak in
    assert out.count("#wl4") == 3  # exactly kid1's three domains
