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


# -- forced safe search -------------------------------------------------------

def policy_with(*modes: str) -> Policy:
    from kosherd.policy import UserPolicy

    return Policy(users=[UserPolicy(uid=1000 + i, username=f"u{i}", mode=m)
                         for i, m in enumerate(modes)])


def test_safesearch_points_search_engines_at_their_safe_hostnames():
    out = dns.render_safesearch(policy_with("dnsfilter"))
    assert "cname=www.google.com,forcesafesearch.google.com" in out
    assert "cname=www.bing.com,strict.bing.com" in out
    assert "cname=duckduckgo.com,safe.duckduckgo.com" in out


def test_safesearch_uses_moderate_youtube_not_strict():
    # Strict hides a great deal of ordinary material, which pushes families
    # to switch the filter off entirely.
    out = dns.render_safesearch(policy_with("filtered"))
    assert "cname=www.youtube.com,restrictmoderate.youtube.com" in out
    assert "restrict.youtube.com" not in out.replace("restrictmoderate.youtube.com", "")


def test_safesearch_applies_to_dnsfilter_filtered_and_whitelist():
    for mode in ("dnsfilter", "filtered", "whitelist"):
        assert "forcesafesearch" in dns.render_safesearch(policy_with(mode)), mode


def test_no_safesearch_when_nobody_needs_it():
    out = dns.render_safesearch(policy_with("unfiltered", "none"))
    assert "cname=" not in out
    assert "No user needs safe search" in out


# -- category blocking at the DNS layer ---------------------------------------

def a_bundle():
    from kosherd.categories import parse

    return parse({"domains": {"adult": ["bad.com"], "social": ["chat.com"],
                              "video": ["watch.com"]}})


def test_uninspected_users_get_their_categories_blocked_in_dns():
    from kosherd.policy import UserPolicy

    policy = Policy(users=[UserPolicy(uid=1001, username="k", mode="dnsfilter",
                                      blocked_categories=["adult"])])
    out = dns.render_category_blocks(policy, a_bundle())
    assert "address=/bad.com/0.0.0.0" in out
    assert "chat.com" not in out


def test_filtered_users_are_left_to_the_proxy():
    # The proxy knows who is asking; DNS does not, so doing it here would
    # over-block everyone on the machine.
    from kosherd.policy import UserPolicy

    policy = Policy(users=[UserPolicy(uid=1001, username="k", mode="filtered",
                                      blocked_categories=["adult"])])
    out = dns.render_category_blocks(policy, a_bundle())
    assert "address=" not in out


def test_mixed_uninspected_profiles_block_the_union():
    from kosherd.policy import UserPolicy

    policy = Policy(users=[
        UserPolicy(uid=1001, username="a", mode="dnsfilter",
                   blocked_categories=["adult"]),
        UserPolicy(uid=1002, username="b", mode="dnsfilter",
                   blocked_categories=["social"]),
    ])
    out = dns.render_category_blocks(policy, a_bundle())
    assert "address=/bad.com/0.0.0.0" in out
    assert "address=/chat.com/0.0.0.0" in out
    assert "watch.com" not in out
