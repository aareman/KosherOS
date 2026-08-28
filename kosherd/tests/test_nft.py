import json
import shutil
import subprocess
from pathlib import Path

import pytest

from kosherd import nft
from kosherd.policy import Policy

EXAMPLE = Path(__file__).parents[2] / "policy" / "examples" / "family.json"


def rendered(policy=None) -> str:
    pol = policy or Policy.from_dict(json.loads(EXAMPLE.read_text()))
    return nft.render(pol, dns_uid=989)


def test_every_user_dispatched_to_its_mode_chain():
    out = rendered()
    assert "1000 : jump mode_dnsfilter" in out
    assert "1001 : jump mode_dnsfilter" in out
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
    pol.users[0].mode = "inspect"
    out = nft.render(pol, dns_uid=989, mitm_uid=988)
    assert f"meta skuid {{ {pol.users[0].uid} }} tcp dport {{ 80, 443 }} redirect to :8080" in out
    # inspected users still get the dnsfilter egress policy and evasion block
    assert f"{pol.users[0].uid} : jump mode_dnsfilter" in out
    # the proxy's own traffic must not be redirected back into itself
    assert "meta skuid { 0, 989, 988 } return" in out


def test_no_mitm_redirect_without_inspected_users():
    out = rendered()
    assert "redirect to :8080" not in out
