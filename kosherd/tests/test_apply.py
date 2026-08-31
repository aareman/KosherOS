"""Turning a policy into running enforcement.

apply_policy() is the moment policy becomes real, and its ordering matters:
a ruleset is syntax-checked before it can replace a working one, the proxy
only runs while someone is inspected, and dnsmasq is restarted rather than
reloaded (SIGHUP re-reads /etc/hosts but not config, so whitelist changes
would silently never take effect).
"""

import json

import pytest

from kosherd import apply as apply_mod
from kosherd.apply import ApplyError, apply_policy, write_mitm_rules
from kosherd.policy import Policy, UserPolicy


class FakeRun:
    """Records subprocess calls; fails the ones named in `fail`."""

    def __init__(self, fail=()):
        self.calls: list[list[str]] = []
        self.fail = fail

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        import subprocess

        failed = any(marker in " ".join(argv) for marker in self.fail)
        return subprocess.CompletedProcess(
            argv, 1 if failed else 0, stdout="", stderr="boom" if failed else "")

    def matching(self, *needles) -> list[list[str]]:
        return [c for c in self.calls
                if all(n in " ".join(c) for n in needles)]

    def index_of(self, *needles) -> int:
        for i, call in enumerate(self.calls):
            if all(n in " ".join(call) for n in needles):
                return i
        raise AssertionError(f"no call matching {needles} in {self.calls}")


@pytest.fixture
def env(tmp_path, monkeypatch):
    """apply_policy against temp paths, with no real system to touch."""
    run = FakeRun()
    monkeypatch.setattr(apply_mod.subprocess, "run", run)
    monkeypatch.setattr(apply_mod, "NFT_RULESET_PATH", tmp_path / "nft" / "kosher.nft")
    monkeypatch.setattr(apply_mod, "DNSMASQ_DROPIN_PATH", tmp_path / "dnsmasq" / "wl.conf")
    monkeypatch.setattr(apply_mod, "SAFESEARCH_PATH", tmp_path / "dnsmasq" / "safesearch.conf")
    monkeypatch.setattr(apply_mod, "CATEGORY_BLOCK_PATH",
                        tmp_path / "dnsmasq" / "categories.conf")
    monkeypatch.setattr(apply_mod, "MITM_DIR", tmp_path / "mitm")
    monkeypatch.setattr(apply_mod, "MITM_RULES_PATH", tmp_path / "mitm" / "rules.json")
    monkeypatch.setattr(apply_mod, "SEARCH_DIR", tmp_path / "search")
    monkeypatch.setattr(apply_mod, "SEARCH_POLICY_PATH",
                        tmp_path / "search" / "policy.json")
    monkeypatch.setattr(apply_mod, "search_uid", lambda: 987, raising=False)
    monkeypatch.setattr(apply_mod, "dnsmasq_uid", lambda: 989)
    monkeypatch.setattr(apply_mod, "mitm_uid", lambda: 988)
    ca_calls = []
    monkeypatch.setattr(apply_mod.mitmca, "ensure_ca", lambda: ca_calls.append(1))
    run.ca_calls = ca_calls
    return run


def policy_with(*modes: str) -> Policy:
    return Policy(users=[
        UserPolicy(uid=1000 + i, username=f"u{i}", mode=mode)
        for i, mode in enumerate(modes)
    ])


# -- the firewall ------------------------------------------------------------

def test_ruleset_is_checked_before_it_is_loaded(env):
    apply_policy(policy_with("filtered"))
    assert env.index_of("nft", "--check") < env.index_of("nft", "-f", "kosher.nft")


def test_a_bad_ruleset_never_replaces_the_working_one(env, tmp_path, monkeypatch):
    monkeypatch.setattr(apply_mod.subprocess, "run", FakeRun(fail=["--check"]))
    with pytest.raises(ApplyError, match="failed nft --check"):
        apply_policy(policy_with("filtered"))
    # Nothing written, nothing loaded: the machine keeps its current rules.
    assert not (tmp_path / "nft" / "kosher.nft").exists()


def test_a_failed_load_is_reported(env, monkeypatch):
    monkeypatch.setattr(apply_mod.subprocess, "run", FakeRun(fail=["nft -f"]))
    with pytest.raises(ApplyError, match="nft load failed"):
        apply_policy(policy_with("filtered"))


def test_ruleset_is_written_root_only(env, tmp_path):
    apply_policy(policy_with("filtered"))
    path = tmp_path / "nft" / "kosher.nft"
    assert path.exists() and (path.stat().st_mode & 0o777) == 0o600


def test_no_temporary_files_are_left_behind(env, tmp_path):
    apply_policy(policy_with("whitelist"))
    for directory in (tmp_path / "nft", tmp_path / "dnsmasq", tmp_path / "mitm"):
        leftovers = [p.name for p in directory.iterdir() if p.name.startswith("tmp")]
        assert leftovers == []


# -- DNS ---------------------------------------------------------------------

def test_dnsmasq_is_restarted_not_reloaded(env):
    # Regression: SIGHUP re-reads /etc/hosts but NOT config files, so new
    # nftset= directives from a whitelist change never took effect.
    apply_policy(policy_with("whitelist"))
    assert env.matching("restart", "kosher-dns.service")
    assert not env.matching("reload", "kosher-dns.service")


def test_dns_failure_does_not_undo_the_firewall(env, monkeypatch, tmp_path):
    monkeypatch.setattr(apply_mod.subprocess,
                        "run", FakeRun(fail=["kosher-dns"]))
    apply_policy(policy_with("whitelist"))  # logged, not raised
    assert (tmp_path / "nft" / "kosher.nft").exists()


# -- the inspection proxy ----------------------------------------------------

def test_proxy_runs_only_while_someone_is_inspected(env):
    apply_policy(policy_with("filtered", "whitelist"))
    assert env.matching("restart", "kosher-mitm.service")
    assert not env.matching("stop", "kosher-mitm.service")


def test_proxy_is_stopped_when_nobody_is_filtered(env):
    apply_policy(policy_with("whitelist", "none"))
    assert env.matching("stop", "kosher-mitm.service")
    assert not env.matching("restart", "kosher-mitm.service")


def test_the_ca_is_prepared_only_for_filtered_mode(env):
    apply_policy(policy_with("whitelist", "none"))
    assert env.ca_calls == []
    apply_policy(policy_with("filtered"))
    assert env.ca_calls == [1]


def test_a_ca_failure_still_leaves_the_firewall_applied(env, monkeypatch, tmp_path):
    def explode():
        raise RuntimeError("no certificate tooling")

    monkeypatch.setattr(apply_mod.mitmca, "ensure_ca", explode)
    apply_policy(policy_with("filtered"))
    assert (tmp_path / "nft" / "kosher.nft").exists()


# -- rules handed to the proxy -----------------------------------------------

def rules_written(tmp_path) -> dict:
    return json.loads((tmp_path / "mitm" / "rules.json").read_text())


def test_only_inspected_users_rules_reach_the_proxy(tmp_path, monkeypatch):
    monkeypatch.setattr(apply_mod, "MITM_DIR", tmp_path / "mitm")
    monkeypatch.setattr(apply_mod, "MITM_RULES_PATH", tmp_path / "mitm" / "rules.json")
    monkeypatch.setattr(apply_mod, "SEARCH_DIR", tmp_path / "search")
    monkeypatch.setattr(apply_mod, "SEARCH_POLICY_PATH",
                        tmp_path / "search" / "policy.json")
    monkeypatch.setattr(apply_mod, "search_uid", lambda: 987, raising=False)
    block = [{"action": "block", "pattern": "x.com"}]
    policy = Policy(users=[
        UserPolicy(uid=1001, username="filtered", mode="filtered", rules=block),
        # Rules on a whitelist user are unenforceable: without interception
        # there is no URL to match, so they must not reach the proxy.
        UserPolicy(uid=1002, username="listed", mode="whitelist", rules=block),
        UserPolicy(uid=1003, username="norules", mode="filtered"),
    ])
    write_mitm_rules(policy)
    assert rules_written(tmp_path) == {
        "1001": {"rules": block, "blocked_categories": [],
                 "media_level": "none", "language_filter": "off",
                  "youtube": {}}}


def test_guest_rules_reach_the_proxy(tmp_path, monkeypatch):
    monkeypatch.setattr(apply_mod, "MITM_DIR", tmp_path / "mitm")
    monkeypatch.setattr(apply_mod, "MITM_RULES_PATH", tmp_path / "mitm" / "rules.json")
    monkeypatch.setattr(apply_mod, "SEARCH_DIR", tmp_path / "search")
    monkeypatch.setattr(apply_mod, "SEARCH_POLICY_PATH",
                        tmp_path / "search" / "policy.json")
    monkeypatch.setattr(apply_mod, "search_uid", lambda: 987, raising=False)
    policy = Policy()
    policy.guest.enabled = True
    policy.guest.uid = 1010
    policy.guest.mode = "filtered"
    policy.guest.rules = [{"action": "block", "pattern": "*"}]
    write_mitm_rules(policy)
    assert "1010" in rules_written(tmp_path)


def test_rules_file_is_readable_by_the_unprivileged_proxy(tmp_path, monkeypatch):
    monkeypatch.setattr(apply_mod, "MITM_DIR", tmp_path / "mitm")
    monkeypatch.setattr(apply_mod, "MITM_RULES_PATH", tmp_path / "mitm" / "rules.json")
    monkeypatch.setattr(apply_mod, "SEARCH_DIR", tmp_path / "search")
    monkeypatch.setattr(apply_mod, "SEARCH_POLICY_PATH",
                        tmp_path / "search" / "policy.json")
    monkeypatch.setattr(apply_mod, "search_uid", lambda: 987, raising=False)
    write_mitm_rules(Policy())
    mode = (tmp_path / "mitm" / "rules.json").stat().st_mode & 0o777
    assert mode == 0o644


# -- surviving a burst of changes --------------------------------------------

def test_restarts_clear_a_previous_failure_first(env):
    # Regression: these services restart on EVERY policy change, so a burst
    # of admin edits tripped systemd's start rate limit and left the
    # resolver dead — the machine then had no DNS at all until someone ran
    # `systemctl reset-failed` by hand.
    apply_policy(policy_with("filtered"))
    for service in ("kosher-dns.service", "kosher-mitm.service"):
        assert env.index_of("reset-failed", service) < env.index_of("restart", service)


def test_stopping_the_proxy_needs_no_reset(env):
    apply_policy(policy_with("whitelist"))
    assert env.matching("stop", "kosher-mitm.service")
    assert not env.matching("reset-failed", "kosher-mitm.service")


def test_repeated_applies_keep_restarting_the_resolver(env):
    # Ten changes in a row is exactly the pattern that broke it.
    for _ in range(10):
        apply_policy(policy_with("whitelist"))
    assert len(env.matching("restart", "kosher-dns.service")) == 10


def test_categories_alone_are_enough_to_reach_the_proxy(tmp_path, monkeypatch):
    # A user with no URL rules but a blocked category still needs an entry,
    # or the proxy would let their traffic through untouched.
    monkeypatch.setattr(apply_mod, "MITM_DIR", tmp_path / "mitm")
    monkeypatch.setattr(apply_mod, "MITM_RULES_PATH", tmp_path / "mitm" / "rules.json")
    monkeypatch.setattr(apply_mod, "SEARCH_DIR", tmp_path / "search")
    monkeypatch.setattr(apply_mod, "SEARCH_POLICY_PATH",
                        tmp_path / "search" / "policy.json")
    monkeypatch.setattr(apply_mod, "search_uid", lambda: 987, raising=False)
    policy = Policy(users=[UserPolicy(uid=1001, username="k", mode="filtered",
                                      blocked_categories=["adult"])])
    write_mitm_rules(policy)
    assert rules_written(tmp_path) == {
        "1001": {"rules": [], "blocked_categories": ["adult"],
                 "media_level": "none", "language_filter": "off",
                  "youtube": {}}}


def test_a_media_level_alone_reaches_the_proxy(tmp_path, monkeypatch):
    # Hiding images needs no URL rules and no categories; without this the
    # user's traffic would pass through untouched.
    monkeypatch.setattr(apply_mod, "MITM_DIR", tmp_path / "mitm")
    monkeypatch.setattr(apply_mod, "MITM_RULES_PATH", tmp_path / "mitm" / "rules.json")
    monkeypatch.setattr(apply_mod, "SEARCH_DIR", tmp_path / "search")
    monkeypatch.setattr(apply_mod, "SEARCH_POLICY_PATH",
                        tmp_path / "search" / "policy.json")
    monkeypatch.setattr(apply_mod, "search_uid", lambda: 987, raising=False)
    policy = Policy(users=[UserPolicy(uid=1001, username="k", mode="filtered",
                                      media_level="all")])
    write_mitm_rules(policy)
    assert rules_written(tmp_path)["1001"]["media_level"] == "all"


def test_youtube_settings_alone_reach_the_proxy(tmp_path, monkeypatch):
    monkeypatch.setattr(apply_mod, "MITM_DIR", tmp_path / "mitm")
    monkeypatch.setattr(apply_mod, "MITM_RULES_PATH", tmp_path / "mitm" / "rules.json")
    monkeypatch.setattr(apply_mod, "SEARCH_DIR", tmp_path / "search")
    monkeypatch.setattr(apply_mod, "SEARCH_POLICY_PATH",
                        tmp_path / "search" / "policy.json")
    monkeypatch.setattr(apply_mod, "search_uid", lambda: 987, raising=False)
    policy = Policy(users=[UserPolicy(uid=1001, username="k", mode="filtered",
                                      youtube={"allowed_channels": ["@torah"]})])
    write_mitm_rules(policy)
    assert rules_written(tmp_path)["1001"]["youtube"] == {"allowed_channels": ["@torah"]}
