"""Ready-made approved-site lists, and what they must contain.

A whitelist account reaches its list and nothing else, so the list has to
include what the sites LOAD as well as the sites themselves: a page whose
fonts and scripts are blocked looks broken rather than blocked, and a
family that sees broken pages turns the filter off. These pin the shipped
bundles, the merge with an account's own list, and that switching one on
actually reaches the resolver.
"""

import json
from pathlib import Path

import pytest

from kosherd import dns, whitelists
from kosherd.policy import GuestPolicy, Policy, PolicyError, UserPolicy

ROOT = Path(__file__).parents[2]
SHIPPED = ROOT / "os-image/files/usr/share/kosher/whitelist-bundles.json"
DOCUMENT = json.loads(SHIPPED.read_text())


@pytest.fixture(autouse=True)
def shipped(monkeypatch):
    """Read the repository's file rather than /usr/share on this machine."""
    monkeypatch.setattr(whitelists, "BUNDLE_PATH", SHIPPED)
    monkeypatch.setattr(whitelists, "OVERRIDE_PATH", Path("/nonexistent"))
    monkeypatch.setattr(whitelists, "_cache", None)


def test_the_two_bundles_a_family_asked_for_are_shipped():
    keys = {b["key"] for b in whitelists.describe()}
    assert {"torah", "mail-and-files"} <= keys


def test_each_bundle_says_what_it_is_and_how_big_it_is():
    for bundle in whitelists.describe():
        assert bundle["label"] and bundle["description"]
        assert bundle["domains"] >= 10


@pytest.mark.parametrize("domain", [
    "sefaria.org", "yutorah.org", "torahanytime.com", "dafyomi.co.il",
    "chabad.org", "hebrewbooks.org", "alhatorah.org", "outorah.org",
])
def test_the_torah_sites_people_name_are_in_the_torah_bundle(domain):
    assert domain in whitelists.domains(["torah"])


@pytest.mark.parametrize("domain", [
    "mail.google.com", "drive.google.com", "outlook.office.com",
    "onedrive.live.com", "dropbox.com", "login.microsoftonline.com",
    "accounts.google.com", "proton.me",
])
def test_the_mail_and_file_sites_are_in_their_bundle(domain):
    assert domain in whitelists.domains(["mail-and-files"])


def test_the_mail_bundle_does_not_open_search_or_video():
    # Entries include their subdomains, so a bare google.com would open
    # search and YouTube along with Gmail.
    opened = whitelists.domains(["mail-and-files"])
    for too_wide in ("google.com", "microsoft.com", "live.com", "youtube.com",
                     "apple.com", "office.net"):
        assert too_wide not in opened, too_wide


def test_every_bundle_brings_the_fonts_and_scripts_pages_need():
    for key in ("torah", "mail-and-files"):
        opened = whitelists.domains([key])
        assert "fonts.gstatic.com" in opened and "cdnjs.cloudflare.com" in opened
    # With nothing chosen, nothing rides along.
    assert whitelists.domains([]) == []


def test_two_bundles_can_be_on_at_once():
    both = whitelists.domains(["torah", "mail-and-files"])
    assert "sefaria.org" in both and "dropbox.com" in both
    assert len(both) > len(whitelists.domains(["torah"]))


def test_every_domain_is_a_bare_host():
    for bundle in DOCUMENT["bundles"] + [DOCUMENT["shared"]]:
        for domain in bundle["domains"]:
            assert domain == domain.strip().lower(), domain
            assert "/" not in domain and ":" not in domain, domain
            assert not domain.startswith(("http", "www.", "*.")), domain
            assert "." in domain, domain


def test_an_accounts_own_list_is_merged_with_its_bundles():
    user = UserPolicy(uid=1001, username="shmuli", mode="whitelist",
                      whitelist=["chinuch.org", "ourfamily.example"],
                      whitelist_bundles=["torah"])
    effective = whitelists.effective(user)
    assert "ourfamily.example" in effective      # the family's own
    assert "sefaria.org" in effective            # the bundle's
    assert effective == sorted(set(effective)), "no duplicates, sorted"


def test_a_bundle_reaches_the_resolver():
    policy = Policy(users=[UserPolicy(uid=1001, username="shmuli", mode="whitelist",
                                      whitelist=["ourfamily.example"],
                                      whitelist_bundles=["torah"])])
    rendered = dns.render(policy)
    assert "/sefaria.org/" in rendered
    assert "/ourfamily.example/" in rendered
    assert "/fonts.gstatic.com/" in rendered


def test_the_guest_can_have_bundles_too():
    policy = Policy(guest=GuestPolicy(enabled=True, uid=1010, mode="whitelist",
                                      whitelist_bundles=["mail-and-files"]))
    assert "/dropbox.com/" in dns.render(policy)


def test_bundles_survive_a_policy_round_trip():
    from kosherd import policy as policy_mod

    policy = Policy(users=[UserPolicy(uid=1001, username="s", mode="whitelist",
                                      whitelist_bundles=["torah"])])
    doc = policy.to_dict()
    policy_mod.validate(doc)
    assert Policy.from_dict(doc).users[0].whitelist_bundles == ["torah"]


def test_the_daemon_refuses_a_list_that_does_not_exist(monkeypatch):
    from kosherd.daemon import Daemon

    daemon = Daemon.__new__(Daemon)
    daemon.policy = Policy(users=[UserPolicy(uid=1001, username="s", mode="whitelist")])
    daemon._save_and_apply = lambda: None
    daemon.impl_SetWhitelistBundles(1001, ["torah"], "")
    assert daemon.policy.users[0].whitelist_bundles == ["torah"]
    with pytest.raises(PolicyError):
        daemon.impl_SetWhitelistBundles(1001, ["torah", "nonsense"], "")
    assert daemon.policy.users[0].whitelist_bundles == ["torah"], "refused, not half-applied"


def test_a_missing_bundle_file_is_not_a_crash(monkeypatch):
    monkeypatch.setattr(whitelists, "BUNDLE_PATH", Path("/nonexistent"))
    monkeypatch.setattr(whitelists, "_cache", None)
    assert whitelists.describe() == []
    assert whitelists.domains(["torah"]) == []
