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
from kosherd import paymentsecurity

# Every filtered account's entry carries the payment and fraud-check hosts
# no category blocks (#265).
_NEVER_BLOCKED = list(paymentsecurity.PAYMENT_SECURITY_HOSTS)


@pytest.fixture(autouse=True)
def _no_nix_on_this_machine(monkeypatch):
    """These tests describe a machine without Nix. Where Nix is installed the
    proxy runs regardless of modes (the build users fetch through it, #123)
    and the rules file carries the builders' entry — test_developer_tools.py
    covers that; here the build group is absent."""
    from kosherd import nixdaemon
    monkeypatch.setattr(nixdaemon, "build_gid", lambda: None)



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
                        tmp_path / "dnsmasq-dnsfilter" / "categories.conf")
    monkeypatch.setattr(apply_mod, "LEGACY_CATEGORY_BLOCK_PATH",
                        tmp_path / "dnsmasq" / "categories.conf")
    monkeypatch.setattr(apply_mod, "ADBLOCK_PATH", tmp_path / "dnsmasq" / "adblock.conf")
    monkeypatch.setattr(apply_mod, "ADBLOCK_OPEN_PATH",
                        tmp_path / "dnsmasq-open" / "adblock.conf")
    monkeypatch.setattr(apply_mod, "MITM_DIR", tmp_path / "mitm")
    monkeypatch.setattr(apply_mod, "STARTED_DIR", tmp_path / "started")
    monkeypatch.setattr(apply_mod.lists, "OVERRIDE_DIR", tmp_path / "lists")
    monkeypatch.setattr(apply_mod, "MITM_RULES_PATH", tmp_path / "mitm" / "rules.json")
    monkeypatch.setattr(apply_mod, "SEARCH_DIR", tmp_path / "search")
    monkeypatch.setattr(apply_mod, "SEARCH_POLICY_PATH",
                        tmp_path / "search" / "policy.json")
    monkeypatch.setattr(apply_mod, "NIX_USERS_PATH",
                        tmp_path / "nix" / "kosheros-users.conf")
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


def test_the_dnsfilter_resolver_runs_only_while_someone_is_on_dnsfilter(env):
    apply_policy(policy_with("dnsfilter", "filtered"))
    assert env.matching("restart", "kosher-dns-dnsfilter.service")
    # After the nft load, which empties the sets its answers fill.
    assert env.index_of("nft", "-f", "kosher.nft") < \
        env.index_of("restart", "kosher-dns-dnsfilter.service")
    env.calls.clear()
    apply_policy(policy_with("filtered", "whitelist"))
    assert env.matching("stop", "kosher-dns-dnsfilter.service")
    assert not env.matching("restart", "kosher-dns-dnsfilter.service")


def test_category_blocks_are_written_for_the_dnsfilter_resolver_only(env, tmp_path):
    # #183: in the family resolver they answered filtered accounts and the
    # proxy too, so a blocked site never reached the proxy's block page.
    legacy = tmp_path / "dnsmasq" / "categories.conf"
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text("address=/bet.example/0.0.0.0\n")
    apply_policy(policy_with("dnsfilter"))
    assert (tmp_path / "dnsmasq-dnsfilter" / "categories.conf").exists()
    assert not legacy.exists()


# -- the inspection proxy ----------------------------------------------------

def test_proxy_runs_only_while_someone_is_inspected(env):
    apply_policy(policy_with("filtered", "whitelist"))
    assert env.matching("restart", "kosher-mitm.service")
    # Its first listener assignment also closes any stale proxy instance.
    assert env.index_of("stop", "kosher-mitm.service") < env.index_of("nft", "-f", "kosher.nft")


def test_a_change_the_proxy_reads_from_its_file_does_not_restart_it(env):
    # Every change used to restart the proxy (and search): seconds before
    # the admin app heard back, and every account's web down meanwhile.
    # Its rules file is re-read as it changes.
    first = policy_with("filtered")
    apply_policy(first)
    assert len(env.matching("restart", "kosher-mitm.service")) == 1
    stops = len(env.matching("stop", "kosher-mitm.service"))
    first.users[0].media_level = "all"
    first.users[0].rules = [{"action": "block", "pattern": "example.com"}]
    apply_policy(first)
    assert len(env.matching("restart", "kosher-mitm.service")) == 1
    assert len(env.matching("stop", "kosher-mitm.service")) == stops
    assert len(env.matching("restart", "kosher-search.service")) == 1
    assert len(env.matching("restart", "kosher-searxng.service")) == 1


def test_swapping_listener_owners_stops_proxy_before_new_firewall(env, tmp_path):
    policy = policy_with("filtered", "whitelist")
    apply_policy(policy)
    assert {uid: entry["port"] for uid, entry in rules_written(tmp_path).items()} == {
        "1000": 30000, "1001": 30001}
    env.calls.clear()
    policy.users[0].mode, policy.users[1].mode = "whitelist", "filtered"
    apply_policy(policy)
    assert {uid: entry["port"] for uid, entry in rules_written(tmp_path).items()} == {
        "1000": 30001, "1001": 30000}
    assert env.index_of("nft", "--check") < env.index_of("stop", "kosher-mitm.service")
    assert env.index_of("stop", "kosher-mitm.service") < env.index_of("nft", "-f", "kosher.nft")
    assert env.index_of("nft", "-f", "kosher.nft") < env.index_of("restart", "kosher-mitm.service")


def test_replacing_account_with_same_listener_count_stops_proxy_before_firewall(env, tmp_path):
    policy = policy_with("filtered")
    apply_policy(policy)
    listen = (tmp_path / "mitm" / "listen.env").read_text()
    env.calls.clear()
    policy.users = [UserPolicy(uid=1005, username="replacement", mode="filtered")]
    apply_policy(policy)
    assert (tmp_path / "mitm" / "listen.env").read_text() == listen
    assert env.index_of("stop", "kosher-mitm.service") < env.index_of("nft", "-f", "kosher.nft")
    assert rules_written(tmp_path)["1005"]["port"] == 30000


def test_failed_stop_preserves_old_firewall_and_listener_ownership(env, tmp_path, monkeypatch):
    policy = policy_with("filtered")
    apply_policy(policy)
    old_firewall = apply_mod.NFT_RULESET_PATH.read_text()
    old_rules = apply_mod.MITM_RULES_PATH.read_text()
    failed = FakeRun(fail=["stop kosher-mitm.service"])
    monkeypatch.setattr(apply_mod.subprocess, "run", failed)
    policy.users[0].uid = 1005
    with pytest.raises(ApplyError, match="before listener reassignment"):
        apply_policy(policy)
    assert not failed.matching("nft", "-f", "kosher.nft")
    assert apply_mod.NFT_RULESET_PATH.read_text() == old_firewall
    assert apply_mod.MITM_RULES_PATH.read_text() == old_rules


@pytest.mark.parametrize("builder,channel", [(999, None), (None, 998), (999, 998)])
def test_unchanged_privileged_listener_identities_do_not_stop_early(env, monkeypatch, builder, channel):
    monkeypatch.setattr(apply_mod.nixdaemon, "build_gid", lambda: builder)
    monkeypatch.setattr(apply_mod, "channel_search_uid", lambda: channel)
    policy = policy_with("filtered")
    apply_policy(policy)
    env.calls.clear()
    policy.users[0].media_level = "all"
    apply_policy(policy)
    assert not env.matching("stop", "kosher-mitm.service")


@pytest.mark.parametrize("before,after", [("dnsfilter", "filtered"), ("whitelist", "filtered"),
                                          ("filtered", "dnsfilter"), ("dnsfilter", "whitelist")])
def test_mode_changes_close_existing_tls_even_when_the_port_is_unchanged(env, tmp_path, before, after):
    policy = policy_with(before)
    apply_policy(policy)
    listen = (tmp_path / "mitm" / "listen.env").read_text()
    stops = len(env.matching("stop", "kosher-mitm.service"))
    policy.users[0].mode = after
    apply_policy(policy)
    assert (tmp_path / "mitm" / "listen.env").read_text() == listen
    assert len(env.matching("restart", "kosher-mitm.service")) == 2
    assert len(env.matching("stop", "kosher-mitm.service")) == stops


def test_removing_a_whitelisted_name_closes_already_open_tls(env):
    policy = policy_with("whitelist")
    policy.users[0].whitelist = ["approved.example", "removed.example"]
    apply_policy(policy)
    policy.users[0].whitelist = ["approved.example"]
    apply_policy(policy)
    assert len(env.matching("restart", "kosher-mitm.service")) == 2


def test_tightening_basic_categories_closes_already_open_tls(env):
    policy = policy_with("dnsfilter")
    apply_policy(policy)
    policy.users[0].blocked_categories = ["adult"]
    apply_policy(policy)
    assert len(env.matching("restart", "kosher-mitm.service")) == 2


@pytest.mark.parametrize("mode", ["filtered", "dnsfilter", "whitelist"])
def test_removing_a_lan_device_closes_existing_upstream_streams(env, mode):
    policy = policy_with(mode)
    policy.users[0].lan_hosts = ["printer.local"]
    apply_policy(policy)
    policy.users[0].lan_hosts = []
    apply_policy(policy)
    assert len(env.matching("restart", "kosher-mitm.service")) == 2


def test_visual_changes_do_not_restart_other_accounts_name_only_streams(env):
    policy = policy_with("filtered", "dnsfilter", "whitelist")
    apply_policy(policy)
    for user in policy.users:
        user.media_level = "all"
        user.cover_style = "skin"
        user.language_filter = "substitute"
        user.calm = {"sites": {"x": {"mode": "single"}}}
        user.rules = [{"action": "block", "pattern": "example.com/path"}]
    # Filtered-mode categories are rechecked per HTTP request, not opaque
    # TLS passthrough, so an ordinary category edit stays responsive too.
    policy.users[0].blocked_categories = ["adult"]
    apply_policy(policy)
    assert len(env.matching("restart", "kosher-mitm.service")) == 1


def test_reordering_name_permissions_does_not_restart_the_proxy(env):
    policy = policy_with("whitelist")
    policy.users[0].whitelist = ["a.example", "b.example"]
    policy.users[0].lan_hosts = ["a.local", "b.local"]
    apply_policy(policy)
    policy.users[0].whitelist.reverse()
    policy.users[0].lan_hosts.reverse()
    apply_policy(policy)
    assert len(env.matching("restart", "kosher-mitm.service")) == 1


def test_failed_connection_policy_restart_does_not_claim_the_change_is_applied(env, monkeypatch):
    policy = policy_with("dnsfilter")
    apply_policy(policy)
    failed = FakeRun(fail=["restart kosher-mitm.service"])
    monkeypatch.setattr(apply_mod.subprocess, "run", failed)
    policy.users[0].blocked_categories = ["adult"]
    with pytest.raises(ApplyError, match="could not restart kosher-mitm"):
        apply_policy(policy)
    assert failed.matching("stop", "kosher-mitm.service")


def test_a_new_listener_restarts_the_proxy(env):
    # A new filtered account needs a port of its own: read only at start.
    apply_policy(policy_with("filtered"))
    apply_policy(policy_with("filtered", "filtered"))
    assert len(env.matching("restart", "kosher-mitm.service")) == 2


def test_edited_lists_restart_the_proxy_and_the_search_front_end(env, tmp_path):
    apply_policy(policy_with("filtered"))
    (tmp_path / "lists").mkdir()
    (tmp_path / "lists" / "wordlist.json").write_text("{}")
    apply_policy(policy_with("filtered"))
    assert len(env.matching("restart", "kosher-mitm.service")) == 2
    assert len(env.matching("restart", "kosher-search.service")) == 2
    assert len(env.matching("restart", "kosher-searxng.service")) == 1, "reads no lists"


def test_a_proxy_that_is_not_running_is_started(env, monkeypatch):
    apply_policy(policy_with("filtered"))
    stopped = FakeRun(fail=["is-active --quiet kosher-mitm"])
    monkeypatch.setattr(apply_mod.subprocess, "run", stopped)
    apply_policy(policy_with("filtered"))
    assert stopped.matching("restart", "kosher-mitm.service")


def test_proxy_is_stopped_when_nobody_needs_web_checks(env):
    apply_policy(policy_with("unfiltered", "none"))
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


def test_content_rules_only_reach_inspected_users_but_all_web_gets_a_listener(tmp_path, monkeypatch):
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
    # Every INSPECTED user is listed — including one with nothing set —
    # because the entry carries the port the proxy identifies them by. The
    # whitelist user is not: nothing of theirs reaches the proxy.
    doc = rules_written(tmp_path)
    named = doc.pop("1002")
    assert named["mode"] == "whitelist"
    assert named["port"] == 30002
    assert named["rules"] == []
    assert named["allowed_hosts"] == []
    assert doc == {
        "1001": {"port": 30000, "rules": block, "blocked_categories": [],
                 "media_level": "none", "cover_style": "frost", "language_filter": "off",
                 "youtube": {}, "calm": {}, "system_allowed": [], "lan_hosts": [],
                 "never_blocked": _NEVER_BLOCKED},
        "1003": {"port": 30001, "rules": [], "blocked_categories": [],
                 "media_level": "none", "cover_style": "frost", "language_filter": "off",
                 "youtube": {}, "calm": {}, "system_allowed": [], "lan_hosts": [],
                 "never_blocked": _NEVER_BLOCKED}}
    listen = (tmp_path / "mitm" / "listen.env").read_text()
    assert listen == ("MITM_MODES=--mode transparent@127.0.0.1:30000 "
                      "--mode transparent@::1:30000 "
                      "--mode transparent@127.0.0.1:30001 "
                      "--mode transparent@::1:30001 "
                      "--mode transparent@127.0.0.1:30002 "
                      "--mode transparent@::1:30002\n")


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
    apply_policy(policy_with("none"))
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
        "1001": {"port": 30000, "rules": [], "blocked_categories": ["adult"],
                 "media_level": "none", "cover_style": "frost", "language_filter": "off",
                 "youtube": {}, "calm": {}, "system_allowed": [], "lan_hosts": [],
                 "never_blocked": _NEVER_BLOCKED}}


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
