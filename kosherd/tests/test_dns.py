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

    # social renders here; adult does NOT — the upstream family resolver
    # (1.1.1.3) blocks adult and malware for every query already, and the
    # full adult list is millions of domains, far past what a dnsmasq
    # config on a low-end machine can carry.
    policy = Policy(users=[UserPolicy(uid=1001, username="k", mode="dnsfilter",
                                      blocked_categories=["adult", "social"])])
    out = dns.render_category_blocks(policy, a_bundle())
    assert "address=/chat.com/0.0.0.0" in out
    assert "bad.com" not in out
    assert "upstream family" in out
    assert "watch.com" not in out


def test_whitelist_users_add_no_dns_category_blocks():
    from kosherd.policy import UserPolicy

    # Default-deny already: nothing a whitelist user blocks needs a DNS
    # entry — and rendering it crashed once the sqlite catalogue landed.
    policy = Policy(users=[UserPolicy(uid=1001, username="w", mode="whitelist",
                                      blocked_categories=["social"])])
    out = dns.render_category_blocks(policy, a_bundle())
    assert "address=" not in out


def test_the_sqlite_catalogue_renders_too(tmp_path):
    # Switching to whitelist/dnsfilter mode raised AttributeError once the
    # real catalogue (sqlite, no .domains dict) shipped: the renderer only
    # knew the JSON bundle's shape.
    import sqlite3

    from kosherd.categories import SqliteBundle
    from kosherd.policy import UserPolicy

    db = sqlite3.connect(tmp_path / "c.sqlite")
    db.executescript(
        "CREATE TABLE domains(domain TEXT, category TEXT,"
        " PRIMARY KEY(domain,category)) WITHOUT ROWID;"
        "CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);")
    db.executemany("INSERT INTO domains VALUES (?,?)",
                   [("bet.com", "gambling"), ("bad.com", "adult")])
    db.commit(); db.close()
    policy = Policy(users=[UserPolicy(uid=1, username="d", mode="dnsfilter",
                                      blocked_categories=["gambling", "adult"])])
    out = dns.render_category_blocks(policy, SqliteBundle(tmp_path / "c.sqlite"))
    assert "address=/bet.com/0.0.0.0" in out
    assert "bad.com" not in out


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
                   blocked_categories=["video"]),
        UserPolicy(uid=1002, username="b", mode="dnsfilter",
                   blocked_categories=["social"]),
    ])
    out = dns.render_category_blocks(policy, a_bundle())
    assert "address=/watch.com/0.0.0.0" in out
    assert "address=/chat.com/0.0.0.0" in out
    assert "bad.com" not in out


# -- the CNAME target has to be one dnsmasq already knows ---------------------

def test_every_safe_search_target_has_an_address():
    """dnsmasq will not chase a cname target upstream.

    `cname=` only accepts a target dnsmasq already knows — from a hosts
    file, from DHCP, or from a host-record. Without a host-record it
    answered www.google.com with a CNAME and no address, so safe search
    did not force safe search: it broke Google outright for every filtered
    account. Verified against a real dnsmasq in `just check-dns`.
    """
    from kosherd.dns import SAFESEARCH_ADDRESSES, SAFESEARCH_CNAMES

    missing = set(SAFESEARCH_CNAMES) - set(SAFESEARCH_ADDRESSES)
    assert not missing, (
        f"{sorted(missing)} would answer a CNAME and nothing else, which "
        "breaks the site rather than making it safe")


def test_the_host_records_come_before_the_cnames():
    from kosherd.policy import Policy, UserPolicy

    out = dns.render_safesearch(Policy(revision=1, users=[
        UserPolicy(uid=1001, username="a", mode="dnsfilter")]))
    assert out.index("host-record=") < out.index("cname=")


def test_each_target_gets_exactly_one_host_record():
    from kosherd.dns import SAFESEARCH_CNAMES
    from kosherd.policy import Policy, UserPolicy

    out = dns.render_safesearch(Policy(revision=1, users=[
        UserPolicy(uid=1001, username="a", mode="dnsfilter")]))
    for target in SAFESEARCH_CNAMES:
        assert out.count(f"host-record={target},") == 1, target


def test_the_addresses_are_addresses():
    import ipaddress

    from kosherd.dns import SAFESEARCH_ADDRESSES

    for target, address in SAFESEARCH_ADDRESSES.items():
        ipaddress.ip_address(address)  # raises if it is not one


def test_nothing_is_rendered_when_no_account_needs_safe_search():
    from kosherd.policy import Policy, UserPolicy

    out = dns.render_safesearch(Policy(revision=1, users=[
        UserPolicy(uid=1001, username="a", mode="unfiltered")]))
    assert "cname=" not in out and "host-record=" not in out
