"""Ads and trackers blocked for everyone, like a Pi-hole.

The machine forces every account's DNS through its own two resolvers, so a
domain list rendered into both of them reaches every browser and every app
on every account — which is what "block all of the ads, for all browsers"
means and what a browser extension could never be. Machine-wide, on by
default, guardian-gated to switch off.
"""

import importlib.util
import json
from pathlib import Path

import pytest

from kosherd import apply as apply_mod
from kosherd import dns
from kosherd.policy import Policy, UserPolicy

ROOT = Path(__file__).parents[2]
FILES = ROOT / "os-image/files"


class _Bundle:
    def __init__(self, ads=("ads.example", "tracker.example"), other=("shop.example",)):
        self.ads, self.other = list(ads), list(other)

    def domains_in(self, wanted):
        out = []
        if "ads" in wanted:
            out += self.ads
        if "shopping" in wanted:
            out += self.other
        return sorted(out)


# -- the setting ------------------------------------------------------------------

def test_ad_blocking_is_on_by_default_and_round_trips():
    assert Policy().adblock is True
    doc = Policy(revision=1).to_dict()
    assert doc["adblock"] == {"enabled": True}
    doc["adblock"]["enabled"] = False
    assert Policy.from_dict(doc).adblock is False


def test_an_older_policy_without_the_field_gets_it_on():
    doc = Policy(revision=1).to_dict()
    del doc["adblock"]
    assert Policy.from_dict(doc).adblock is True


# -- the resolvers ----------------------------------------------------------------

def test_the_drop_in_makes_every_ad_domain_not_exist():
    out = dns.render_adblock(Policy(revision=3), _Bundle())
    assert "address=/ads.example/" in out
    assert "address=/tracker.example/" in out
    # NXDOMAIN form: one line per domain, no address.
    assert "0.0.0.0" not in out
    assert "shop.example" not in out, "only the advertising category"


def test_switched_off_the_drop_in_blocks_nothing():
    policy = Policy(revision=3)
    policy.adblock = False
    out = dns.render_adblock(policy, _Bundle())
    assert "address=" not in out
    assert "off" in out


def test_the_category_blocks_do_not_list_ads_twice():
    # A dnsfilter account that also blocks the ads category would otherwise
    # carry the list twice in one resolver, doubling its memory.
    user = UserPolicy(uid=1001, username="k", mode="dnsfilter",
                      blocked_categories=["ads", "shopping"])
    policy = Policy(revision=1, users=[user])
    out = dns.render_category_blocks(policy, _Bundle())
    assert "shop.example" in out
    assert "ads.example" not in out
    policy.adblock = False
    out = dns.render_category_blocks(policy, _Bundle())
    assert "ads.example" in out, "with ad blocking off the category is theirs alone"


def test_the_same_drop_in_goes_to_both_resolvers(tmp_path, monkeypatch):
    # The plain resolver serves unfiltered accounts. Ad blocking is not
    # content filtering, so they get it too — that is what makes this a
    # Pi-hole and not a per-account setting.
    monkeypatch.setattr(apply_mod, "ADBLOCK_PATH", tmp_path / "a" / "adblock.conf")
    monkeypatch.setattr(apply_mod, "ADBLOCK_OPEN_PATH", tmp_path / "b" / "adblock.conf")
    monkeypatch.setattr(apply_mod, "CATEGORY_BLOCK_PATH", tmp_path / "a" / "categories.conf")
    monkeypatch.setattr(apply_mod, "DNSMASQ_DROPIN_PATH", tmp_path / "a" / "whitelist.conf")
    monkeypatch.setattr(apply_mod, "SAFESEARCH_PATH", tmp_path / "a" / "safesearch.conf")
    monkeypatch.setattr(apply_mod, "NFT_RULESET_PATH", tmp_path / "kosher.nft")
    monkeypatch.setattr(apply_mod, "MITM_DIR", tmp_path / "mitm")
    monkeypatch.setattr(apply_mod, "MITM_RULES_PATH", tmp_path / "mitm" / "rules.json")
    monkeypatch.setattr(apply_mod, "SEARCH_DIR", tmp_path / "search")
    monkeypatch.setattr(apply_mod, "SEARCH_POLICY_PATH", tmp_path / "search" / "policy.json")
    monkeypatch.setattr(apply_mod, "dnsmasq_uid", lambda: 989)
    monkeypatch.setattr(apply_mod, "mitm_uid", lambda: 988)
    monkeypatch.setattr(apply_mod, "search_uid", lambda: 987)
    monkeypatch.setattr(apply_mod.categories_mod, "load_any", lambda *a: _Bundle())
    monkeypatch.setattr(apply_mod.subprocess, "run",
                        lambda *a, **k: type("R", (), {"returncode": 0, "stderr": ""})())
    apply_mod.apply_policy(Policy(revision=1, users=[
        UserPolicy(uid=1001, username="k", mode="unfiltered")]))
    family = (tmp_path / "a" / "adblock.conf").read_text()
    plain = (tmp_path / "b" / "adblock.conf").read_text()
    assert family == plain
    assert "address=/ads.example/" in plain


def test_the_plain_resolver_reads_its_drop_in_directory():
    conf = (FILES / "etc/kosher/dnsmasq-open.conf").read_text()
    assert "conf-dir=/etc/kosher/dnsmasq-open.d/,*.conf" in conf
    assert dns.ADBLOCK_OPEN_CONF.startswith("/etc/kosher/dnsmasq-open.d/")
    # dnsmasq refuses to start on a conf-dir that does not exist, so the
    # image ships the directory.
    assert any((FILES / "etc/kosher/dnsmasq-open.d").glob("*.conf"))


def test_firefox_cannot_step_around_the_resolver_with_doh():
    pol = json.loads((FILES / "etc/firefox/policies/policies.json").read_text())
    assert pol["policies"]["DNSOverHTTPS"] == {"Enabled": False, "Locked": True}


# -- the list -----------------------------------------------------------------------

spec = importlib.util.spec_from_file_location("fetch_categories", ROOT / "scripts/fetch-categories.py")
fetch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fetch)


def test_the_ads_category_is_fed_by_pi_holes_default_list():
    labels = [label for label, _url in fetch.PLAIN_SOURCES["ads"]]
    assert any("StevenBlack" in label for label in labels)


def test_a_hosts_file_parses_to_domains_and_nothing_else():
    text = """# Title: StevenBlack/hosts
127.0.0.1 localhost
127.0.0.1 localhost.localdomain
::1 ip6-localhost
0.0.0.0 0.0.0.0
0.0.0.0 ads.example  # trailing comment
0.0.0.0 Tracker.Example
bare-list.example
"""
    assert fetch.plain_domains(text) == ["ads.example", "tracker.example", "bare-list.example"]
