"""Tailscale (#30): the resolvers' tailnet forward, the status the admin
app reads, the sign-in, and the reasoning that keeps the filter in front
of a tailnet."""

from __future__ import annotations

import os
import stat
import time

import pytest

from kosherd import dns, nft, tailnet
from kosherd.categories import parse
from kosherd.policy import Policy, UserPolicy


# -- the resolvers ------------------------------------------------------------

def test_the_tailnet_is_forwarded_to_tailscales_resolver():
    out = dns.render_tailnet("tail230de1.ts.net")
    assert "server=/tail230de1.ts.net/100.100.100.100" in out.splitlines()


def test_only_this_tailnet_is_forwarded_not_all_of_ts_net():
    # tailscaled sends a name it does not own back to the system resolver,
    # which is dnsmasq: forwarding all of ts.net would loop on somebody
    # else's tailnet.
    out = dns.render_tailnet("tail230de1.ts.net.")
    assert "server=/ts.net/" not in out
    assert "server=/tail230de1.ts.net/100.100.100.100" in out


@pytest.mark.parametrize("suffix", [None, "", "   "])
def test_nothing_is_forwarded_when_not_signed_in(suffix):
    out = dns.render_tailnet(suffix)
    assert "server=" not in out
    assert out.startswith("# Rendered by kosherd")


@pytest.mark.parametrize("suffix", [
    "tail.ts.net/1.2.3.4\naddress=/google.com/", "a b.ts.net", "ts", "x/y.ts.net",
    "../etc", "-bad.ts.net",
])
def test_a_name_that_is_not_a_dns_name_never_becomes_a_directive(suffix):
    out = dns.render_tailnet(suffix)
    assert "server=" not in out and "address=" not in out
    assert len(out.splitlines()) == 2


def test_tailscales_own_domain_is_never_blocked_at_the_resolver():
    # tailscale.com is in the "proxy" list in the shipped catalogue, and
    # proxy is blocked by default. The resolver answers root too, so the
    # block would stop tailscaled reaching its control server.
    bundle = parse({"domains": {"proxy": ["tailscale.com", "login.tailscale.com",
                                          "vpn.example", "nottailscale.com"]}})
    policy = Policy(users=[UserPolicy(uid=1001, username="k", mode="dnsfilter",
                                      blocked_categories=["proxy"])])
    out = dns.render_category_blocks(policy, bundle)
    assert "address=/vpn.example/0.0.0.0" in out
    assert "address=/nottailscale.com/0.0.0.0" in out
    assert "tailscale.com/0.0.0.0" not in out.replace("nottailscale.com", "")


def test_both_resolvers_get_the_forward_and_only_changed_ones_restart(tmp_path):
    family, plain = tmp_path / "dnsmasq.d", tmp_path / "dnsmasq-open.d"
    family.mkdir()
    plain.mkdir()
    paths = (family / "tailnet.conf", plain / "tailnet.conf")
    restarted = []

    assert tailnet.sync_dns("tail230de1.ts.net", paths=paths, restart=restarted.append)
    assert restarted == [tailnet.DNS_SERVICE, tailnet.OPEN_DNS_SERVICE]
    for path in paths:
        assert "server=/tail230de1.ts.net/100.100.100.100" in path.read_text()
        assert stat.S_IMODE(path.stat().st_mode) == 0o644

    restarted.clear()
    assert not tailnet.sync_dns("tail230de1.ts.net", paths=paths, restart=restarted.append)
    assert restarted == []  # nothing changed, so nobody's cache is dropped

    assert tailnet.sync_dns(None, paths=paths, restart=restarted.append)
    assert all("server=" not in p.read_text() for p in paths)


# -- what the admin app reads ------------------------------------------------

SIGNED_IN = {
    "BackendState": "Running",
    "AuthURL": "",
    "TailscaleIPs": ["100.101.102.103", "fd7a:115c:a1e0::1"],
    "Self": {"DNSName": "kosheros.tail230de1.ts.net.", "UserID": 42,
             "TailscaleIPs": ["100.101.102.103", "fd7a:115c:a1e0::1"], "Online": True},
    "MagicDNSSuffix": "tail230de1.ts.net",
    "CurrentTailnet": {"Name": "avi@example.com", "MagicDNSSuffix": "tail230de1.ts.net",
                       "MagicDNSEnabled": True},
    "User": {"42": {"LoginName": "avi@example.com", "DisplayName": "Avi"}},
    "Peer": {"nodekey:abc": {"DNSName": "git.tail230de1.ts.net."}},
}


def test_a_signed_in_computer_says_who_where_and_its_addresses():
    got = tailnet.describe(SIGNED_IN)
    assert got["signed_in"] and got["connected"]
    assert got["user"] == "avi@example.com"
    assert got["tailnet"] == "avi@example.com"
    assert got["dns_suffix"] == "tail230de1.ts.net"
    assert got["name"] == "kosheros.tail230de1.ts.net"
    assert got["addresses"][0] == "100.101.102.103"
    # The tailnet's other machines are not the admin app's business.
    assert "git.tail230de1" not in repr(got)


def test_a_computer_waiting_for_sign_in_is_not_signed_in():
    got = tailnet.describe({"BackendState": "NeedsLogin",
                            "AuthURL": "https://login.tailscale.com/a/abc123",
                            "CurrentTailnet": None, "Self": {}})
    assert not got["signed_in"] and not got["connected"]
    assert got["login_url"] == "https://login.tailscale.com/a/abc123"
    assert got["dns_suffix"] == "" and got["addresses"] == []


def test_a_stopped_tailnet_is_signed_in_but_not_connected():
    got = tailnet.describe({**SIGNED_IN, "BackendState": "Stopped"})
    assert got["signed_in"] and not got["connected"]


def test_the_sign_in_address_is_found_in_what_tailscale_prints():
    printed = ["", "To authenticate, visit:", "",
               "\thttps://login.tailscale.com/a/1a2b3c4d5e", ""]
    assert tailnet.login_url_from(printed) == "https://login.tailscale.com/a/1a2b3c4d5e"
    assert tailnet.login_url_from(["Success."]) is None


def test_sign_in_never_lets_tailscaled_near_dns_ssh_or_the_firewall():
    flags = set(tailnet.UP_FLAGS)
    assert "--accept-dns=false" in flags
    assert "--ssh=false" in flags
    assert "--shields-up" in flags
    assert "--netfilter-mode=off" in flags
    assert "--reset" in flags  # nothing set by hand outlives a sign-in
    assert not any(f.startswith("--operator") for f in flags)


# -- signing in, against a pretend tailscale ---------------------------------

def fake_tailscale(tmp_path, monkeypatch, script: str):
    exe = tmp_path / "tailscale"
    exe.write_text("#!/bin/sh\n" + script)
    exe.chmod(0o755)
    monkeypatch.setattr(tailnet, "TAILSCALE", str(exe))
    return exe


def test_sign_in_returns_the_address_and_keeps_waiting(tmp_path, monkeypatch):
    args_file = tmp_path / "args"
    fake_tailscale(tmp_path, monkeypatch, f"""
echo "$@" > {args_file}
echo
echo "To authenticate, visit:"
echo
echo "	https://login.tailscale.com/a/feedface"
sleep 0.3
echo Success.
""")
    finished = []
    url = tailnet.sign_in(on_finished=finished.append)
    assert url == "https://login.tailscale.com/a/feedface"
    args = args_file.read_text().split()
    assert args[0] == "up" and "--accept-dns=false" in args
    deadline = time.time() + 5
    while not finished and time.time() < deadline:
        time.sleep(0.05)
    assert finished == [True]


def test_a_computer_still_signed_in_needs_no_address(tmp_path, monkeypatch):
    fake_tailscale(tmp_path, monkeypatch, "exit 0\n")
    assert tailnet.sign_in() == ""


def test_a_failed_sign_in_says_why(tmp_path, monkeypatch):
    fake_tailscale(tmp_path, monkeypatch,
                   'echo "backend error: control server unreachable"; exit 1\n')
    with pytest.raises(tailnet.TailnetError, match="control server unreachable"):
        tailnet.sign_in()


def test_status_when_tailscaled_is_down(tmp_path, monkeypatch):
    fake_tailscale(tmp_path, monkeypatch,
                   'echo "failed to connect to local tailscaled" >&2; exit 1\n')
    got = tailnet.status()
    assert got["state"] == "Unavailable" and not got["signed_in"]
    assert "tailscaled" in got["error"]


def test_status_without_tailscale_installed(tmp_path, monkeypatch):
    monkeypatch.setattr(tailnet, "TAILSCALE", str(tmp_path / "missing"))
    assert tailnet.status() == {"installed": False, "state": "NotInstalled",
                                "signed_in": False, "connected": False}
    with pytest.raises(tailnet.TailnetError):
        tailnet.sign_in()


# -- the filter stays in front of the tailnet --------------------------------

def ruleset() -> str:
    policy = Policy(users=[UserPolicy(uid=1001, username="kid", mode="filtered"),
                           UserPolicy(uid=1002, username="teen", mode="dnsfilter")])
    return nft.render(policy, dns_uid=990, mitm_uid=991)


def chain(text: str, name: str) -> str:
    start = text.index(f"chain {name} {{")
    return text[start:text.index("\n    }", start)]


def test_a_filtered_accounts_redirect_does_not_depend_on_the_interface():
    # The redirect into the proxy is matched by owner and port in the
    # output hook. A packet routed to tailscale0, to a tailnet machine or
    # through an exit node, is redirected the same as one routed to the
    # Wi-Fi: no rule here names an outgoing interface but loopback.
    redirect = chain(ruleset(), "dns_redirect")
    assert "meta skuid 1001 tcp dport != " in redirect
    lines = [l for l in redirect.splitlines() if "oif" in l]
    assert lines == ['        oif "lo" return']


def test_nothing_on_the_tailnet_is_let_in():
    # Inbound is the input chain's policy drop. Nothing names tailscale0,
    # and tailscaled's own rules (which accept anything on it) are kept out
    # by --netfilter-mode=off.
    text = ruleset()
    assert "tailscale0" not in text
    assert "policy drop;" in chain(text, "input")
    assert "41641" not in text
