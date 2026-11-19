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
    monkeypatch.setattr(apply_mod, "LEGACY_CATEGORY_BLOCK_PATH", tmp_path / "old" / "categories.conf")
    monkeypatch.setattr(apply_mod, "DNSMASQ_DROPIN_PATH", tmp_path / "a" / "whitelist.conf")
    monkeypatch.setattr(apply_mod, "SAFESEARCH_PATH", tmp_path / "a" / "safesearch.conf")
    monkeypatch.setattr(apply_mod, "NFT_RULESET_PATH", tmp_path / "kosher.nft")
    monkeypatch.setattr(apply_mod, "MITM_DIR", tmp_path / "mitm")
    monkeypatch.setattr(apply_mod, "STARTED_DIR", tmp_path / "started")
    monkeypatch.setattr(apply_mod, "MITM_RULES_PATH", tmp_path / "mitm" / "rules.json")
    monkeypatch.setattr(apply_mod, "SEARCH_DIR", tmp_path / "search")
    monkeypatch.setattr(apply_mod, "SEARCH_POLICY_PATH", tmp_path / "search" / "policy.json")
    monkeypatch.setattr(apply_mod, "NIX_USERS_PATH", tmp_path / "nix" / "kosheros-users.conf")
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


# F4 (#185): Chromium is a Flatpak from the Store, and nothing told it to
# keep DNS-over-HTTPS off. These are the paths each Chromium-based browser
# on Flathub reads: its own /etc path on the host (Chrome, Edge and Brave
# link those into the sandbox at start), and for the Chromium Flatpaks an
# extension point in the system installation that points at /etc/chromium.
CHROMIUM_POLICIES = (
    "etc/chromium/policies/managed/kosheros.json",
    "etc/opt/chrome/policies/managed/kosheros.json",
    "etc/opt/edge/policies/managed/kosheros.json",
    "etc/brave/policies/managed/kosheros.json",
)


@pytest.mark.parametrize("path", CHROMIUM_POLICIES)
def test_chromium_based_browsers_keep_doh_off(path):
    policy = json.loads((FILES / path).read_text())
    assert policy["DnsOverHttpsMode"] == "off"
    assert policy["EncryptedClientHelloEnabled"] is False


@pytest.mark.parametrize("app", [
    "org.chromium.Chromium",
    "io.github.ungoogled_software.ungoogled_chromium",
])
def test_chromium_flatpaks_are_pointed_at_the_policy(app):
    lines = (FILES / "usr/lib/tmpfiles.d/kosher-browser-policy.conf").read_text().splitlines()
    link = f"/var/lib/flatpak/extension/{app}.Extension.system-policies/x86_64/1"
    entries = [line.split() for line in lines if line and not line.startswith("#")]
    assert ["L+", link, "-", "-", "-", "-", "/etc/chromium"] in entries
    # tmpfiles makes the parents too, in order, so the link has somewhere to go.
    made = [e[1] for e in entries if e[0] == "d"]
    assert f"/var/lib/flatpak/extension/{app}.Extension.system-policies/x86_64" in made


# -- the list -----------------------------------------------------------------------

spec = importlib.util.spec_from_file_location("fetch_categories", ROOT / "scripts/fetch-categories.py")
fetch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fetch)


def test_the_ads_category_is_fed_by_many_lists_not_one_merged_file():
    # It used to fetch StevenBlack's merged unified-hosts file. That file
    # aggregates sixteen lists, two of which forbid commercial use, and our
    # category database is an adaptation of UT1's CC BY-SA 4.0 data —
    # ShareAlike forces the whole database to permit commercial use, so
    # nothing forbidding it can be inside. See docs/licensing.md.
    sources = fetch.PLAIN_SOURCES["ads"]
    assert len(sources) >= 10, "the ad coverage collapsed to a handful of lists"
    urls = [url for _label, url in sources]
    assert not any(url.rstrip("/").endswith("StevenBlack/hosts/master/hosts")
                   for url in urls), \
        "the merged unified-hosts file is back; it cannot be licensed"


def test_no_noncommercial_ad_list_can_creep_back_in():
    # Named explicitly rather than checked by licence string, because the
    # failure mode is somebody re-adding one of these two by URL.
    urls = " ".join(url for _label, url in fetch.PLAIN_SOURCES["ads"]).lower()
    for forbidden in ("mvps.org", "someonewhocares.org"):
        assert forbidden not in urls, \
            f"{forbidden} is NonCommercial and conflicts with CC BY-SA"


def test_every_ad_list_records_the_licence_it_is_used_under():
    # Attribution is a condition of several of these licences, so the label
    # is where the product carries it. A source with no licence in its
    # label is a source nobody checked.
    for label, _url in fetch.PLAIN_SOURCES["ads"]:
        assert "(" in label and ")" in label, \
            f"{label!r} does not say what licence it is used under"


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


# -- what it never blocks: a checkout's fraud check (#265) -------------------------
#
# ThreatMetrix fingerprints the browser so Lowe's and Synchrony can approve
# a card. It is on every tracker list, so it was in our ads category, and
# the resolver answered NXDOMAIN for it; with ad blocking off, the proxy
# refused the same host at its category step. Lowe's returned 400 to the
# order and emptied the cart, four times, and the activity feed showed
# nothing. These hosts, and what an administrator adds, stay reachable in
# both layers.

from kosherd import access, cli, paymentsecurity
from kosherd.daemon import Daemon
from kosherd.policy import PolicyError


class _TrackerBundle(_Bundle):
    def __init__(self):
        super().__init__(ads=("ads.example", "online-metrix.net", "h.online-metrix.net",
                              "fp.shop-check.example", "tracker.example"))


def test_payment_security_hosts_are_left_reachable_although_the_list_carries_them():
    out = dns.render_adblock(Policy(revision=3), _TrackerBundle())
    assert "address=/ads.example/" in out
    assert "online-metrix.net" not in out, "ThreatMetrix is a checkout's fraud check"
    assert "address=/tracker.example/" in out
    assert "2 left reachable" in out


def test_an_administrators_addition_is_left_reachable_too():
    policy = Policy(revision=3)
    policy.adblock_allowed = ["*.shop-check.example"]
    out = dns.render_adblock(policy, _TrackerBundle())
    assert "shop-check.example" not in out
    assert "address=/tracker.example/" in out


def test_the_category_blocks_leave_them_reachable_as_well():
    # A dnsfilter account that blocks ads with the machine-wide blocker
    # off gets the ads list from the category drop-in instead; the same
    # hosts stay out of it.
    user = UserPolicy(uid=1001, username="k", mode="dnsfilter", blocked_categories=["ads"])
    policy = Policy(revision=1, users=[user])
    policy.adblock = False
    policy.adblock_allowed = ["*.shop-check.example"]
    out = dns.render_category_blocks(policy, _TrackerBundle())
    assert "ads.example" in out
    assert "online-metrix.net" not in out
    assert "shop-check.example" not in out


def test_the_built_in_set_names_the_services_that_broke_the_checkout():
    for host in ("h.online-metrix.net", "imgs.signifyd.com",
                 "w2txo5aa.g.aa.online-metrix.net"):
        assert paymentsecurity.covers(host, paymentsecurity.PAYMENT_SECURITY_HOSTS), host
    # Not a blanket pass for anything on a tracker list.
    for host in ("edge.fullstory.com", "wa.onelink.me", "ads.example", ""):
        assert not paymentsecurity.covers(host, paymentsecurity.PAYMENT_SECURITY_HOSTS), host


def test_the_proxy_is_told_the_hosts_for_every_filtered_account(tmp_path, monkeypatch):
    monkeypatch.setattr(apply_mod, "MITM_DIR", tmp_path)
    monkeypatch.setattr(apply_mod, "MITM_RULES_PATH", tmp_path / "rules.json")
    monkeypatch.setattr(apply_mod.nixdaemon, "build_gid", lambda: None)
    policy = Policy(revision=1, users=[
        UserPolicy(uid=1001, username="k", mode="filtered", blocked_categories=["ads"]),
        UserPolicy(uid=1002, username="u", mode="unfiltered")])
    policy.adblock_allowed = ["*.shop-check.example"]
    apply_mod.write_mitm_rules(policy, ())
    doc = json.loads((tmp_path / "rules.json").read_text())
    assert list(doc) == ["1001"]
    never = doc["1001"]["never_blocked"]
    assert "*.online-metrix.net" in never and "*.signifyd.com" in never
    assert never[-1] == "*.shop-check.example"
    # The tailnet's names keep their own key; nothing moved.
    assert doc["1001"]["system_allowed"] == []


# -- the setting ------------------------------------------------------------------

def test_the_additions_round_trip_and_an_older_policy_has_none():
    policy = Policy(revision=1)
    policy.adblock_allowed = ["*.shop-check.example"]
    doc = policy.to_dict()
    assert doc["adblock"] == {"enabled": True, "allowed": ["*.shop-check.example"]}
    assert Policy.from_dict(doc).adblock_allowed == ["*.shop-check.example"]
    # Nothing added: the key is absent, as it was before, so the document
    # an older kosherd wrote still validates and still reads.
    assert Policy(revision=1).to_dict()["adblock"] == {"enabled": True}
    assert Policy.from_dict({**doc, "adblock": {"enabled": False}}).adblock_allowed == []


def test_the_document_with_additions_validates():
    from kosherd import policy as policy_mod

    policy = Policy(revision=1)
    policy.adblock_allowed = ["*.shop-check.example", "fp.other.example"]
    policy_mod.validate(policy.to_dict())


def _daemon(monkeypatch):
    daemon = Daemon.__new__(Daemon)
    daemon.policy = Policy(revision=1)
    applied = []
    monkeypatch.setattr(Daemon, "_save_and_apply", lambda self: applied.append(True))
    return daemon, applied


def test_setting_additions_records_them_once_without_the_built_in_ones(monkeypatch):
    daemon, applied = _daemon(monkeypatch)
    daemon.impl_SetAdBlockAllowed(
        ["FP.Shop-Check.example", "*.online-metrix.net", "fp.shop-check.example",
         "https://check.other.example/path"], "")
    assert daemon.policy.adblock_allowed == ["check.other.example", "fp.shop-check.example"]
    assert applied


def test_a_bad_entry_changes_nothing(monkeypatch):
    daemon, applied = _daemon(monkeypatch)
    daemon.policy.adblock_allowed = ["fp.shop-check.example"]
    with pytest.raises(PolicyError):
        daemon.impl_SetAdBlockAllowed(["other.example", "not a host"], "")
    assert daemon.policy.adblock_allowed == ["fp.shop-check.example"]
    assert not applied


def test_the_call_is_a_guardian_gated_filter_change():
    assert access.ACTIONS["SetAdBlockAllowed"] == access.ACTION_MANAGE_FILTER
    assert "SetAdBlockAllowed" in access.GUARDIAN_GATED
    assert "SetAdBlockAllowed" in access.CHANGES
    assert "SetAdBlockAllowed" not in access.PER_USER


# -- kosherctl adblock ------------------------------------------------------------

class _AdblockClient:
    def __init__(self, allowed=()):
        self.allowed = list(allowed)
        self.calls = []

    def get_policy(self):
        return {"adblock": {"enabled": True, "allowed": self.allowed}}

    def guardian_enabled(self):
        return False

    def set_adblock(self, on, pw=""):
        self.calls.append(("set", on, pw))

    def set_adblock_allowed(self, hosts, pw=""):
        self.calls.append(("allowed", list(hosts), pw))


def _adblock(*argv):
    return cli.main(["adblock", *argv])


def test_on_and_off_still_work(monkeypatch):
    client = _AdblockClient()
    monkeypatch.setattr(cli, "_client", lambda: client)
    assert _adblock("off", "--guardian-password", "pw") == 0
    assert _adblock("on") == 0
    assert client.calls == [("set", False, "pw"), ("set", True, "")]


def test_allow_adds_to_what_was_there_and_disallow_takes_it_back(monkeypatch, capsys):
    client = _AdblockClient(allowed=["fp.shop-check.example"])
    monkeypatch.setattr(cli, "_client", lambda: client)
    assert _adblock("allow", "Check.Other.example", "fp.shop-check.example") == 0
    assert client.calls[-1] == ("allowed", ["fp.shop-check.example", "check.other.example"], "")
    assert _adblock("disallow", "fp.shop-check.example") == 0
    assert client.calls[-1] == ("allowed", [], "")
    assert "check.other.example" in capsys.readouterr().out


def test_a_built_in_host_cannot_be_disallowed_and_the_list_is_shown(monkeypatch, capsys):
    client = _AdblockClient(allowed=["fp.shop-check.example"])
    monkeypatch.setattr(cli, "_client", lambda: client)
    assert _adblock("disallow", "*.online-metrix.net") == 2
    assert client.calls == []
    assert "built in" in capsys.readouterr().err
    assert _adblock("allowed") == 0
    out = capsys.readouterr().out
    assert "*.online-metrix.net" in out and "fp.shop-check.example" in out
    assert _adblock("allow") == 2, "nothing named"
    assert _adblock("allow", "not a host") == 2


def test_firefox_keeps_hostname_checks_visible_without_ech_grease():
    policy = json.loads((FILES / "etc/firefox/policies/policies.json").read_text())["policies"]
    assert policy["DisableEncryptedClientHello"] is True
