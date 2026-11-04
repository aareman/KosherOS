"""The activity log's grants (GrantUrl): a page, its folder or its whole
site, allowed, with its pictures shown, or bypassed."""

import pytest

from kosherd import access, urlrules
from kosherd.daemon import Daemon
from kosherd.policy import Policy, PolicyError, UserPolicy


def _daemon(monkeypatch, mode="filtered", rules=()):
    daemon = Daemon.__new__(Daemon)
    daemon.policy = Policy(revision=1, users=[
        UserPolicy(uid=1001, username="kid", mode=mode, rules=list(rules))])
    monkeypatch.setattr(Daemon, "_save_and_apply", lambda self: None)
    return daemon


URL = "https://www.shop.example/library/article/123?ref=x"


def test_the_folder_of_a_page():
    assert urlrules.folder_of(URL) == "shop.example/library/article/*"
    assert urlrules.folder_of("https://shop.example/a") is None
    assert urlrules.folder_of("https://shop.example/") is None
    assert urlrules.folder_of("https://shop.example/a/") == "shop.example/a/*"


@pytest.mark.parametrize("scope, pattern", [
    ("page", "shop.example/library/article/123"),
    ("folder", "shop.example/library/article/*"),
    ("site", "www.shop.example"),
])
def test_pictures_for_a_page_its_folder_or_the_site(monkeypatch, scope, pattern):
    daemon = _daemon(monkeypatch, rules=[{"action": "block", "pattern": "*"}])
    daemon.impl_GrantUrl(1001, URL, "pictures", scope, "")
    rules = daemon.policy.user(1001).rules
    assert rules[0] == {"action": "pictures", "pattern": pattern}, "ahead of the rest"
    assert rules[1] == {"action": "block", "pattern": "*"}
    action, _ = urlrules.decide(urlrules.parse_rules(rules), URL.split("?")[0])
    assert action == "pictures"


def test_a_folder_rule_reaches_the_pages_beside_it_and_no_others(monkeypatch):
    daemon = _daemon(monkeypatch)
    daemon.impl_GrantUrl(1001, URL, "pictures", "folder", "")
    rules = urlrules.parse_rules(daemon.policy.user(1001).rules)
    assert urlrules.decide(rules, "https://shop.example/library/article/9")[0] == "pictures"
    assert urlrules.decide(rules, "https://shop.example/library/other/9")[0] == "allow"


def test_a_page_at_the_top_has_no_folder_so_the_folder_is_the_site(monkeypatch):
    daemon = _daemon(monkeypatch)
    daemon.impl_GrantUrl(1001, "https://shop.example/d", "pictures", "folder", "")
    assert daemon.policy.user(1001).rules == [{"action": "pictures", "pattern": "shop.example"}]


def test_a_later_grant_for_the_same_place_replaces_the_earlier(monkeypatch):
    daemon = _daemon(monkeypatch)
    daemon.impl_GrantUrl(1001, URL, "allow", "site", "")
    daemon.impl_GrantUrl(1001, URL, "pictures", "site", "")
    assert daemon.policy.user(1001).rules == [
        {"action": "pictures", "pattern": "www.shop.example"}]


def test_pictures_and_bypass_need_a_filtered_account(monkeypatch):
    daemon = _daemon(monkeypatch, mode="dnsfilter")
    for level in ("pictures", "bypass"):
        with pytest.raises(PolicyError):
            daemon.impl_GrantUrl(1001, URL, level, "page", "")
    daemon.impl_GrantUrl(1001, URL, "allow", "page", "")


def test_unknown_levels_and_scopes_are_refused(monkeypatch):
    daemon = _daemon(monkeypatch)
    with pytest.raises(PolicyError):
        daemon.impl_GrantUrl(1001, URL, "block", "page", "")
    with pytest.raises(PolicyError):
        daemon.impl_GrantUrl(1001, URL, "pictures", "everywhere", "")


def test_the_grant_is_a_filter_change():
    assert access.ACTIONS["GrantUrl"] == access.ACTION_MANAGE_FILTER
    assert "GrantUrl" in access.GUARDIAN_GATED
    assert "GrantUrl" in access.CHANGES
    assert "GrantUrl" in access.PER_USER


def test_a_pictures_rule_is_valid_policy():
    policy = Policy(revision=1, users=[UserPolicy(
        uid=1001, username="kid", mode="filtered",
        rules=[{"action": "pictures", "pattern": "shop.example"}])])
    again = Policy.from_dict(policy.to_dict())
    assert again.user(1001).rules[0]["action"] == "pictures"
