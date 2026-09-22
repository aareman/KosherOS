"""Every port a supervised account opens goes through the proxy, or is named.

Finding F1 of the security review (issue #33): the filtered modes ended in
`accept`, so only 80 and 443 reached the proxy and only 53 the resolver; a
web server on any other port, or anything that was not web, left the
machine unread. Now every TCP port a filtered account opens is redirected
into its proxy listener, the non-web protocols a family needs are named,
loopback is exempt, and UDP — which cannot be inspected — is a decision:
video calls on by default, with a switch.
"""

import json

import pytest

from kosherd import nft
from kosherd.daemon import Daemon, PolicyError
from kosherd.policy import Policy, UserPolicy


def _policy(**user_kwargs) -> Policy:
    user = dict(uid=1001, username="kid", mode="filtered")
    user.update(user_kwargs)
    return Policy(revision=1, users=[UserPolicy(**user)])


def _render(policy: Policy) -> str:
    return nft.render(policy, dns_uid=988, mitm_uid=987)


def _at(rules: list[str], prefix: str) -> int:
    """Index of the first rule starting with `prefix` (the vmap line carries
    its whole mapping, so an exact match would have to spell it out)."""
    return next(i for i, rule in enumerate(rules) if rule.startswith(prefix))


def _chain(out: str, name: str) -> list[str]:
    """The rules of one chain, by brace depth (sets have braces too), without
    comments or the type/hook line."""
    start = out.index(f"chain {name} {{") + len(f"chain {name} {{")
    depth, i = 1, start
    while depth:
        depth += (out[i] == "{") - (out[i] == "}")
        i += 1
    lines = [line.strip() for line in out[start:i - 1].splitlines()]
    return [l for l in lines if l and not l.startswith("#") and not l.startswith("type ")]


# ---- what is redirected -------------------------------------------------------

def test_every_tcp_port_of_a_filtered_account_is_redirected_to_its_listener():
    out = _render(_policy())
    assert "meta skuid 1001 tcp dport != { 53, 465, 587, 993 } redirect to :30000" in out


def test_loopback_is_never_redirected_and_comes_first():
    # A dev server on 127.0.0.1:443, or the search page, must not be sent
    # into the proxy; the return is the first rule so nothing precedes it.
    rules = _chain(_render(_policy()), "dns_redirect")
    assert rules[0] == 'oif "lo" return'


def test_an_accounts_extra_ports_are_excluded_from_the_redirect_and_accepted():
    out = _render(_policy(extra_ports=[22, 2222]))
    assert "tcp dport != { 22, 53, 465, 587, 993, 2222 } redirect to :30000" in out
    assert "meta skuid 1001 tcp dport { 22, 2222 } accept" in out
    # ...and before the mode dispatch, so the mode chain's reject never sees them
    output = _chain(out, "output")
    assert output.index("meta skuid 1001 tcp dport { 22, 2222 } accept") \
        < _at(output, "meta skuid vmap")


# ---- what the mode chains do with what is left ----------------------------------

def test_filtered_mode_is_default_deny():
    rules = _chain(_render(_policy()), "mode_filtered")
    assert rules[-1] == "reject"
    assert "tcp dport { 465, 587, 993 } accept" in rules      # mail, by name
    assert "udp dport { 123 } accept" in rules                 # time
    assert f"udp dport >= {nft.UDP_HIGH} accept" in rules      # video calls
    assert "accept" not in rules[-1]


def test_dnsfilter_mode_allows_the_web_and_mail_and_refuses_the_rest():
    rules = _chain(_render(_policy(mode="dnsfilter")), "mode_dnsfilter")
    assert "tcp dport { 80, 443, 465, 587, 993 } accept" in rules
    assert rules[-1] == "reject"


def test_video_calls_off_rejects_high_udp_for_that_account_only():
    out = _render(Policy(revision=1, users=[
        UserPolicy(uid=1001, username="kid", mode="filtered", video_calls=False),
        UserPolicy(uid=1002, username="teen", mode="dnsfilter"),
    ]))
    assert f"meta skuid {{ 1001 }} udp dport >= {nft.UDP_HIGH} reject" in out
    output = _chain(out, "output")
    assert output.index(f"meta skuid {{ 1001 }} udp dport >= {nft.UDP_HIGH} reject") \
        < _at(output, "meta skuid vmap")
    # the mode chain still allows it for everyone else
    assert f"udp dport >= {nft.UDP_HIGH} accept" in _chain(out, "mode_filtered")


def test_video_calls_on_by_default_renders_no_reject():
    assert f"udp dport >= {nft.UDP_HIGH} reject" not in _render(_policy())


def test_local_services_are_kept_for_every_mode_before_dispatch():
    output = _chain(_render(_policy()), "output")
    assert "jump local_services" in output
    assert output.index("jump local_services") < _at(output, "meta skuid vmap")


def test_the_named_ports_are_protocols_a_family_needs():
    # Mail and nothing else by default. A new entry here is a decision.
    assert nft.DIRECT_TCP_PORTS == (465, 587, 993)
    assert nft.UDP_LOW_ALLOWED == (123,)


def test_the_ruleset_still_loads(tmp_path):
    # The syntax check the other suite runs, with every new rule in play.
    import shutil, subprocess
    if not shutil.which("nft") or not shutil.which("unshare"):
        pytest.skip("nft/unshare not available")
    ruleset = _render(Policy(revision=1, users=[
        UserPolicy(uid=1001, username="kid", mode="filtered", extra_ports=[22]),
        UserPolicy(uid=1002, username="teen", mode="dnsfilter", video_calls=False),
        UserPolicy(uid=1003, username="wl", mode="whitelist"),
    ]))
    path = tmp_path / "k.nft"; path.write_text(ruleset)
    res = subprocess.run(["unshare", "-rn", "nft", "--check", "-f", str(path)],
                         capture_output=True, text=True)
    assert res.returncode == 0, res.stderr


# ---- the policy round trip ---------------------------------------------------------

def test_the_new_fields_round_trip_and_omit_their_defaults():
    on = UserPolicy(uid=1, username="a", mode="filtered")
    assert "video_calls" not in on.to_dict() and "extra_ports" not in on.to_dict()
    off = UserPolicy(uid=1, username="a", mode="filtered", video_calls=False, extra_ports=[2222, 22, 22])
    d = off.to_dict()
    assert d["video_calls"] is False and d["extra_ports"] == [22, 2222]


# ---- the daemon method ---------------------------------------------------------------

def _daemon(monkeypatch):
    daemon = Daemon.__new__(Daemon)
    daemon.policy = _policy()
    applied = []
    monkeypatch.setattr(Daemon, "_save_and_apply", lambda self: applied.append(True))
    return daemon, applied


def test_setting_network_access_records_both_fields_and_applies(monkeypatch):
    daemon, applied = _daemon(monkeypatch)
    daemon.impl_SetNetworkAccess(1001, False, json.dumps([2222, 22]), "")
    user = daemon.policy.account(1001)
    assert user.video_calls is False and user.extra_ports == [22, 2222]
    assert applied


@pytest.mark.parametrize("ports", ["[53]", "[80]", "[443]"])
def test_the_filters_own_ports_cannot_be_opened_directly(monkeypatch, ports):
    # 53 is the resolver's, 80 and 443 the proxy's: granting them would
    # silently undo the filter for that account.
    daemon, _ = _daemon(monkeypatch)
    with pytest.raises(PolicyError, match="filter's own"):
        daemon.impl_SetNetworkAccess(1001, True, ports, "")


@pytest.mark.parametrize("ports", ["[0]", "[65536]", "[\"22\"]", "not json", "[true]"])
def test_bad_port_lists_are_refused(monkeypatch, ports):
    daemon, applied = _daemon(monkeypatch)
    with pytest.raises(PolicyError):
        daemon.impl_SetNetworkAccess(1001, True, ports, "")
    assert not applied


def test_an_unmanaged_account_is_refused(monkeypatch):
    daemon, _ = _daemon(monkeypatch)
    with pytest.raises(PolicyError, match="not managed"):
        daemon.impl_SetNetworkAccess(4242, True, "[]", "")
