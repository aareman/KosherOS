import json
import shutil
import subprocess
from pathlib import Path

import pytest

from kosherd import nft
from kosherd.policy import Policy

EXAMPLE = Path(__file__).parents[2] / "policy" / "examples" / "family.json"


def _example() -> Policy:
    return Policy.from_dict(json.loads(EXAMPLE.read_text()))


def rendered(policy=None) -> str:
    pol = policy or Policy.from_dict(json.loads(EXAMPLE.read_text()))
    return nft.render(pol, dns_uid=989)


def test_every_user_dispatched_to_its_mode_chain():
    out = rendered()
    assert "1000 : jump mode_filtered" in out
    assert "1001 : jump mode_filtered" in out
    assert "1002 : jump mode_whitelist" in out
    assert "1003 : jump mode_none" in out


def test_unknown_users_fail_closed():
    out = rendered()
    # after the vmap, everything falls through to mode_none
    assert out.index("meta skuid vmap") < out.index("jump mode_none")


def test_empty_policy_still_fails_closed():
    out = rendered(Policy())
    assert "vmap" not in out
    assert "jump mode_none" in out


def test_dns_redirect_exempts_only_root_and_dnsmasq():
    out = rendered()
    assert "meta skuid { 0, 989 } return" in out
    assert "udp dport 53 redirect to :53" in out
    assert "tcp dport 53 redirect to :53" in out


def test_evasion_block_kills_dot_quic_doh():
    out = rendered()
    assert "tcp dport 853 reject" in out
    assert "udp dport 853 reject" in out
    assert "udp dport 443 reject" in out
    assert "ip daddr @doh4 reject" in out
    # unfiltered public resolvers are in the DoH block set
    assert "1.1.1.1" in out and "8.8.8.8/31" in out


def test_whitelist_mode_only_reaches_wl_and_sys_sets():
    out = rendered()
    wl = out.split("chain mode_whitelist")[1].split("chain")[0]
    assert "ip daddr @wl4 tcp dport { 80, 443 } accept" in wl
    assert "ip daddr @sys4 tcp dport { 80, 443 } accept" in wl
    rules = [line.strip() for line in wl.splitlines() if line.strip() and line.strip() != "}"]
    assert rules[-1] == "reject"


def test_system_uids_unrestricted():
    assert "meta skuid < 1000 accept" in rendered()


def test_established_accepted_before_uid_dispatch():
    out = rendered()
    chain = out.split("chain output")[1].split("chain")[0]
    # The dispatch, specifically: per-account rules that must precede the
    # loopback accepts (the plain resolver's guard, #127) also say skuid.
    assert chain.index("ct state established,related accept") < chain.index("meta skuid vmap")


def test_input_chain_drops_new_inbound():
    out = rendered()
    chain = out.split("chain input")[1].split("chain")[0]
    assert "policy drop;" in chain
    assert "ct state established,related accept" in chain


@pytest.mark.skipif(shutil.which("nft") is None, reason="nft binary not installed")
def test_ruleset_passes_nft_check(tmp_path):
    f = tmp_path / "kosher.nft"
    f.write_text(rendered())
    # nft needs a netlink socket even for --check; give it a private user+net
    # namespace so this runs unprivileged.
    res = subprocess.run(
        ["unshare", "-rn", "nft", "--check", "-f", str(f)], capture_output=True, text=True
    )
    if "unshare" in res.stderr and res.returncode != 0:
        pytest.skip(f"cannot create user/net namespace here: {res.stderr.strip()}")
    assert res.returncode == 0, res.stderr


def test_inspect_mode_redirects_web_to_mitmproxy():
    pol = Policy.from_dict(json.loads(EXAMPLE.read_text()))
    pol.users[0].mode = "filtered"
    out = nft.render(pol, dns_uid=989, mitm_uid=988)
    inspected = sorted(u.uid for u in pol.effective_users() if u.mode == "filtered")
    # One rule per user, each to their OWN port: the port tells the proxy
    # who is connecting.
    ports = nft.mitm_ports(pol)
    for uid in inspected:
        assert f"meta skuid {uid} tcp dport != {{ 21, 22, 25, 53, 110, 143, 465, 587, 993, 22000, 22067 }} redirect to :{ports[uid]}" in out
    assert len(set(ports.values())) == len(inspected), "ports must be distinct"
    # inspected users still get the filtered egress policy and evasion block
    for uid in inspected:
        assert f"{uid} : jump mode_filtered" in out
    # the proxy's own traffic must not be redirected back into itself
    assert "meta skuid { 0, 989, 988 } return" in out


def test_no_mitm_redirect_without_filtered_users():
    """Nobody filtered means no traffic is decrypted at all."""
    pol = Policy.from_dict(json.loads(EXAMPLE.read_text()))
    for user in pol.users:
        if user.mode == "filtered":
            user.mode = "whitelist"
    pol.guest.mode = "whitelist"
    out = nft.render(pol, dns_uid=989, mitm_uid=988)
    assert "redirect to :3" not in out  # no per-user proxy port at all


# -- the five modes -----------------------------------------------------------

def policy_of(*modes: str) -> Policy:
    from kosherd.policy import UserPolicy

    return Policy(users=[UserPolicy(uid=1000 + i, username=f"u{i}", mode=m)
                         for i, m in enumerate(modes)])


def test_every_mode_has_a_chain():
    from kosherd.policy import MODES

    out = nft.render(policy_of(*MODES), dns_uid=989, mitm_uid=988)
    for i, mode in enumerate(MODES):
        assert f"{1000 + i} : jump {nft.MODE_CHAINS[mode]}" in out


def test_only_filtered_traffic_is_decrypted():
    out = nft.render(policy_of("dnsfilter", "filtered", "unfiltered"),
                     dns_uid=989, mitm_uid=988)
    # uid 1001 is the filtered one; nobody else is redirected to the proxy.
    assert "meta skuid 1001 tcp dport != { 21, 22, 25, 53, 110, 143, 465, 587, 993, 22000, 22067 } redirect to :30000" in out


def test_unfiltered_users_get_the_plain_resolver():
    out = nft.render(policy_of("dnsfilter", "unfiltered"), dns_uid=989, mitm_uid=988)
    assert f"meta skuid {{ 1001 }} udp dport 53 redirect to :{nft.OPEN_DNS_PORT}" in out
    # ...and everyone else still lands on the filtering resolver.
    assert "udp dport 53 redirect to :53" in out


def test_unfiltered_mode_does_not_block_evasion():
    out = nft.render(policy_of("unfiltered"), dns_uid=989, mitm_uid=988)
    chain = out.split("chain mode_unfiltered")[1].split("chain")[0]
    assert "evasion_block" not in chain


# -- the search service -------------------------------------------------------

def test_the_search_backend_is_closed_to_human_users():
    # Loopback is otherwise wide open, which is what lets a user reach the
    # search page at all. The engine behind it must stay shut: its raw
    # results carry the snippets the filter exists to withhold.
    ruleset = nft.render(_example(), dns_uid=989, search_uid=987)
    line = f'oif "lo" tcp dport {nft.SEARCH_BACKEND_PORT} meta skuid >= {nft.UID_MIN} reject'
    assert line in ruleset
    # and it must come before loopback is accepted, or it never runs
    assert ruleset.index(line) < ruleset.index('oif "lo" accept')


def test_the_search_service_resolves_names_without_being_redirected():
    ruleset = nft.render(_example(), dns_uid=989, search_uid=987)
    assert "meta skuid { 0, 989, 987 } return" in ruleset


def test_without_a_search_user_nothing_about_search_is_rendered():
    ruleset = nft.render(_example(), dns_uid=989)
    assert str(nft.SEARCH_BACKEND_PORT) not in ruleset


def test_user_ports_are_deterministic_and_shared_by_every_renderer():
    # The same function feeds the ruleset, the proxy's rules file and its
    # listener list, so a uid can never be redirected to a port nobody is
    # listening on, or answered by the wrong user's policy.
    pol = policy_of("filtered", "filtered", "unfiltered")
    ports = nft.mitm_ports(pol)
    assert ports == {1000: 30000, 1001: 30001}
    assert nft.mitm_ports(pol) == ports
    out = nft.render(pol, dns_uid=989, mitm_uid=988)
    for uid, port in ports.items():
        assert f"meta skuid {uid} tcp dport != {{ 21, 22, 25, 53, 110, 143, 465, 587, 993, 22000, 22067 }} redirect to :{port}" in out


# -- protocols where the server speaks first (#164) ----------------------------

def _dev_policy(*modes: str, developer=True) -> Policy:
    from kosherd.policy import UserPolicy

    return Policy(users=[UserPolicy(uid=1000 + i, username=f"u{i}", mode=m,
                                    developer_tools=developer)
                         for i, m in enumerate(modes)])


def test_ssh_is_not_sent_into_the_proxy():
    # In the proxy an SSH connection hangs for good: mitmproxy waits for the
    # client's first bytes, the client waits for the server's banner.
    out = nft.render(policy_of("filtered"), dns_uid=989, mitm_uid=988)
    redirect = [line for line in out.splitlines() if "redirect to :30000" in line]
    assert len(redirect) == 1
    assert 22 in nft.SERVER_FIRST_TCP_PORTS
    excluded = redirect[0].split("!= {")[1].split("}")[0]
    assert {int(p) for p in excluded.split(",")} >= set(nft.SERVER_FIRST_TCP_PORTS)


def test_ssh_without_developer_tools_is_refused_not_accepted():
    out = nft.render(policy_of("filtered", "dnsfilter"), dns_uid=989, mitm_uid=988)
    output = out.split("chain output")[1].split("chain input")[0]
    # nothing lets 22 out, so the mode chain's reject answers it at once
    assert "dport 22" not in output and "@git4" not in output
    filtered = out.split("chain mode_filtered {")[1].split("\n    }")[0]
    rules = [l.strip() for l in filtered.splitlines() if l.strip() and not l.strip().startswith("#")]
    assert rules[-1] == "reject"
    assert not any("22" in r for r in rules)


def test_a_developer_account_reaches_the_git_hosts_over_ssh_only():
    out = nft.render(_dev_policy("filtered", "dnsfilter", "whitelist", "unfiltered"),
                     dns_uid=989, mitm_uid=988)
    output = out.split("chain output")[1].split("chain input")[0]
    v4 = "meta skuid { 1000, 1001, 1002 } ip daddr @git4 tcp dport 22 accept"
    v6 = "meta skuid { 1000, 1001, 1002 } ip6 daddr @git6 tcp dport 22 accept"
    assert v4 in output and v6 in output
    # before the mode dispatch, whose chains would refuse it
    assert output.index(v4) < output.index("meta skuid vmap")
    # the sets exist, and port 22 is not opened to any other address
    assert "set git4 {" in out and "set git6 {" in out
    assert "tcp dport 22 accept" not in output.replace(v4, "").replace(v6, "")


def test_developer_tools_off_means_no_git_ssh():
    out = nft.render(_dev_policy("filtered", developer=False), dns_uid=989, mitm_uid=988)
    assert "@git4 tcp dport 22" not in out


@pytest.mark.skipif(shutil.which("nft") is None, reason="nft binary not installed")
def test_the_git_ssh_ruleset_passes_nft_check(tmp_path):
    f = tmp_path / "kosher.nft"
    f.write_text(nft.render(_dev_policy("filtered", "dnsfilter"), dns_uid=989, mitm_uid=988))
    res = subprocess.run(
        ["unshare", "-rn", "nft", "--check", "-f", str(f)], capture_output=True, text=True
    )
    if "unshare" in res.stderr and res.returncode != 0:
        pytest.skip(f"cannot create user/net namespace here: {res.stderr.strip()}")
    assert res.returncode == 0, res.stderr


# -- the dnsfilter accounts' own resolver (#183) and addresses (#189, #192) --

def test_dnsfilter_accounts_get_their_own_resolver():
    out = nft.render(policy_of("dnsfilter", "filtered"), dns_uid=989, mitm_uid=988)
    chain = out[out.index("chain dns_redirect"):out.index("chain output")]
    for proto in ("udp", "tcp"):
        rule = f"meta skuid {{ 1000 }} {proto} dport 53 redirect to :{nft.DNSFILTER_DNS_PORT}"
        assert rule in chain
        # Ahead of the loopback return: asking 127.0.0.1:53 by hand must
        # not reach the family resolver, which holds no category blocks.
        assert chain.index(rule) < chain.index('oif "lo" return')
    # The filtered account is not sent there: the proxy decides its
    # categories, and needs the real address to show the block page.
    assert "meta skuid { 1001 }" not in chain.split("oif")[0]


def test_no_dnsfilter_resolver_redirect_without_a_dnsfilter_account():
    out = nft.render(policy_of("filtered", "whitelist"), dns_uid=989, mitm_uid=988)
    assert f"redirect to :{nft.DNSFILTER_DNS_PORT}" not in out


def test_a_dnsfilter_accounts_container_uses_the_same_resolver():
    out = nft.render(policy_of("dnsfilter"), dns_uid=989,
                     subids={1000: [(524288, 589823)]})
    assert (f"meta skuid {{ 1000, 524288-589823 }} udp dport 53 redirect to "
            f":{nft.DNSFILTER_DNS_PORT}") in out


def _mode_chain(out: str, name: str) -> list[str]:
    body = out[out.index(f"chain {name} {{"):]
    body = body[:body.index("\n    }")]
    return [l.strip() for l in body.splitlines()[1:]
            if l.strip() and not l.strip().startswith("#")]


def test_dnsfilter_connects_only_to_addresses_its_resolver_handed_out():
    rules = _mode_chain(nft.render(policy_of("dnsfilter"), dns_uid=989), "mode_dnsfilter")
    accepts = [r for r in rules if "tcp dport" in r and r.endswith("accept")]
    assert accepts, rules
    # Every TCP accept names a set dnsmasq fills: a bare address nobody
    # looked up matches none of them and falls to the final reject.
    for rule in accepts:
        assert any(f"daddr @{s}" in rule for s in
                   ("dns4", "dns6", "wl4", "wl6", "sys4", "sys6", "git4", "git6",
                    "st4", "st6")), rule
    # dnsmasq feeds a name's most specific nftset= line only, so a name on
    # another list lands in that list's set and not in dns4: github.com
    # over HTTPS must still open for the account (#168 put it in git4).
    for s in ("wl4", "sys4", "git4"):
        assert any(f"ip daddr @{s} tcp dport" in r for r in accepts), s
    assert rules[-1] == "reject"


def test_the_safe_search_addresses_are_in_the_dns_set():
    # dnsmasq answers those from its own host-records and adds nothing to a
    # set for them; without this, safe search would break Google outright.
    from kosherd.dns import SAFESEARCH_ADDRESSES

    out = nft.render(policy_of("dnsfilter"), dns_uid=989)
    block = out[out.index("set dns4 {"):]
    block = block[:block.index("}\n")]
    for address in SAFESEARCH_ADDRESSES.values():
        assert address in block


def test_dnsfilter_never_reaches_the_home_network():
    rules = _mode_chain(nft.render(policy_of("dnsfilter"), dns_uid=989), "mode_dnsfilter")
    lan = rules.index("ip daddr @lan4 reject")
    lan6 = next(i for i, r in enumerate(rules) if r.startswith("ip6 daddr { fc00::/7"))
    first_tcp = next(i for i, r in enumerate(rules) if "tcp dport" in r)
    assert lan < first_tcp and lan6 < first_tcp
    # The local services every mode keeps are let through before the mode
    # chain, so printing and mDNS still work.
    out = nft.render(policy_of("dnsfilter"), dns_uid=989)
    output = out[out.index("chain output {"):]
    assert output.index("jump local_services") < output.index("meta skuid vmap")


def test_a_nix_build_may_ask_the_family_resolver():
    # #190: the builders' loopback was refused whole, the resolver with it,
    # so no build resolved a name and none reached the proxy.
    out = nft.render(policy_of("filtered"), dns_uid=989, mitm_uid=988, build_gid=999)
    output = out[out.index("chain output {"):]
    for proto in ("udp", "tcp"):
        rule = f"meta skgid 999 ip daddr 127.0.0.1 {proto} dport 53 accept"
        assert rule in output
        assert output.index(rule) < output.index('meta skgid 999 oif "lo" reject')
    # ...and only that resolver: the plain one stays out of reach.
    assert f"meta skgid 999 ip daddr 127.0.0.1 udp dport {nft.OPEN_DNS_PORT}" not in out


@pytest.mark.skipif(shutil.which("nft") is None, reason="nft binary not installed")
def test_a_ruleset_with_every_mode_and_nix_passes_nft_check(tmp_path):
    from kosherd.policy import MODES

    f = tmp_path / "kosher.nft"
    f.write_text(nft.render(policy_of(*MODES), dns_uid=989, mitm_uid=988,
                            search_uid=987, build_gid=999,
                            subids={1002: [(524288, 589823)]}))
    res = subprocess.run(
        ["unshare", "-rn", "nft", "--check", "-f", str(f)], capture_output=True, text=True
    )
    if "unshare" in res.stderr and res.returncode != 0:
        pytest.skip(f"cannot create user/net namespace here: {res.stderr.strip()}")
    assert res.returncode == 0, res.stderr
