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
    assert chain.index("ct state established,related accept") < chain.index("meta skuid")


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
        assert f"meta skuid {uid} tcp dport {{ 80, 443 }} redirect to :{ports[uid]}" in out
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
    assert "meta skuid 1001 tcp dport { 80, 443 } redirect to :30000" in out


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
        assert f"meta skuid {uid} tcp dport {{ 80, 443 }} redirect to :{port}" in out
