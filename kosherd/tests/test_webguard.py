"""F2/F4/F5/F6: request names, resolver shapes and upstream destinations.

The two names use the very same initial destination. These tests measure
which name reaches the upstream hook, not just whether an IP set is filled.
"""
import asyncio
import json
from types import SimpleNamespace as NS

import pytest

from kosherd import nft, webguard
from kosherd.policy import Policy, UserPolicy
from test_mitm_addon import addon, _request_flow, _shop_filter


@pytest.mark.parametrize("host", ["2130706433", "0x08080808", "0177.0.0.1", "127.1", "134744072",
                                  "８.８.８.８", "２１３０７０６４３３"])
def test_legacy_numeric_ip_spellings_are_not_dns_names(addon, tmp_path, host):
    assert webguard.hostname(host) == ""
    assert addon.is_bare_address(host)
    for mode in ("dnsfilter", "filtered"):
        filt = filtering(addon, tmp_path, mode=mode)
        request = flow(f"http://{host}/")
        filt.request(request)
        assert request.response is not None and request.response.status_code == 403


def filtering(addon, tmp_path, mode="whitelist", rules=()):
    path = tmp_path / "rules.json"
    path.write_text(json.dumps({"1001": {
        "port": 30000, "mode": mode, "rules": list(rules),
        "allowed_hosts": ["approved.example"], "lan_hosts": ["printer.example"],
        "blocked_categories": ["adult"],
    }}))
    filt = _shop_filter(addon)
    filt.policy = addon.PolicyCache(path)
    return filt


def flow(url):
    f = _request_flow(url)
    f.client_conn.proxy_mode = NS(custom_listen_port=30000)
    f.client_conn.error = None
    f.server_conn = NS(address=("93.184.216.34", 443), id="upstream", connected=False, error=None)
    return f


def hello(f, name, *, extensions=(), alpn=(b"h2", b"http/1.1")):
    return NS(context=NS(client=f.client_conn, server=f.server_conn),
              client_hello=NS(sni=name, extensions=extensions, alpn_protocols=alpn),
              ignore_connection=False)


def connect(filt, f, addresses=("93.184.216.34",)):
    async def run():
        loop = asyncio.get_running_loop()
        calls = []
        async def resolve(host, port, **kwargs):
            calls.append((host, port))
            return [(2, 1, 6, "", (a, port)) for a in addresses]
        with pytest.MonkeyPatch.context() as m:
            m.setattr(loop, "getaddrinfo", resolve)
            await filt.server_connect(NS(server=f.server_conn, client=f.client_conn))
        return calls
    return asyncio.run(run())


def test_same_address_different_http_host_is_held_to_the_accounts_list(addon, tmp_path):
    filt = filtering(addon, tmp_path)
    allowed = flow("http://approved.example/page")
    refused = flow("http://unapproved.example/page")
    filt.request(allowed)
    filt.request(refused)
    assert allowed.response is None
    assert allowed.metadata["kosher_name_only"] is True
    assert allowed.server_conn.address == ("approved.example", 80)
    assert refused.response.status_code == 403


def test_names_are_not_borrowed_from_another_account(addon, tmp_path):
    filt = filtering(addon, tmp_path)
    doc = json.loads(filt.policy.path.read_text())
    doc["1002"] = {"port": 30001, "mode": "whitelist", "allowed_hosts": ["other.example"]}
    filt.policy.path.write_text(json.dumps(doc))
    refused = flow("http://other.example/")
    filt.request(refused)
    assert refused.response.status_code == 403


def test_tls_passes_unchanged_only_after_approving_sni(addon, tmp_path):
    filt = filtering(addon, tmp_path)
    allowed = flow("https://approved.example/")
    event = hello(allowed, "approved.example")
    filt.tls_clienthello(event)
    assert event.ignore_connection is True  # upstream certificate, no local CA
    assert allowed.server_conn.error is None
    assert allowed.server_conn.address == ("approved.example", 443)
    assert connect(filt, allowed) == [("approved.example", 443)]
    assert allowed.server_conn.address == ("93.184.216.34", 443)


@pytest.mark.parametrize("sni,extensions,alpn", [
    ("unapproved.example", (), (b"h2",)),
    (None, (), (b"h2",)),
    ("93.184.216.34", (), (b"h2",)),
    ("approved.example", [(0xfe0d, b"ech")], (b"h2",)),
    ("approved.example", (), (b"dot",)),
])
def test_tls_missing_hidden_or_disallowed_name_never_reaches_upstream(addon, tmp_path, sni, extensions, alpn):
    filt = filtering(addon, tmp_path)
    f = flow("https://approved.example/")
    event = hello(f, sni, extensions=extensions, alpn=alpn)
    filt.tls_clienthello(event)
    assert f.server_conn.error
    assert connect(filt, f) == []


def test_name_only_mode_refuses_raw_connections_without_a_checked_host(addon, tmp_path):
    filt = filtering(addon, tmp_path)
    f = flow("https://approved.example/")
    assert connect(filt, f) == []
    assert "no approved" in f.server_conn.error


def test_basic_protection_checks_sni_against_categories(addon, tmp_path):
    filt = filtering(addon, tmp_path, "dnsfilter")
    filt.categories = NS(blocked_categories_of=lambda host, _blocked: {"adult"} if host == "blocked.example" else set())
    for host, blocked in [("ordinary.example", False), ("blocked.example", True)]:
        f = flow("https://" + host)
        filt.tls_clienthello(hello(f, host))
        assert bool(f.server_conn.error) is blocked


@pytest.mark.parametrize("addresses", [
    ["192.168.1.1"], ["127.0.0.1"], ["::1"], ["::ffff:127.0.0.1"],
    ["fd00::1"], ["169.254.169.254"], ["93.184.216.34", "10.0.0.1"],
])
def test_hostname_resolution_cannot_rebind_to_private_network(addon, tmp_path, addresses):
    filt = filtering(addon, tmp_path, "filtered")
    f = flow("http://ordinary.example/")
    filt.request(f)
    connect(filt, f, addresses)
    assert f.server_conn.error


def test_specific_site_rule_keeps_the_admins_printer_escape_hatch(addon, tmp_path):
    filt = filtering(addon, tmp_path, "filtered", [{"action": "allow", "pattern": "printer.example/*"}])
    f = flow("http://printer.example/status")
    filt.request(f)
    connect(filt, f, ["192.168.1.10"])
    assert f.server_conn.error is None
    assert f.server_conn.address == ("192.168.1.10", 80)


def test_blanket_allow_is_not_permission_for_every_lan_device(addon, tmp_path):
    filt = filtering(addon, tmp_path, "filtered", [{"action": "allow", "pattern": "*"}])
    f = flow("http://router.example/")
    filt.request(f)
    connect(filt, f, ["192.168.1.1"])
    assert f.server_conn.error


@pytest.mark.parametrize("path,headers", [
    ("/dns-query?dns=AAAB", {}), ("/dns-query/", {}), ("/%64ns-query", {}),
    ("/resolve", {"content-type": "application/dns-message; charset=binary"}),
    ("/resolve", {"accept": "text/plain, application/dns-message;q=1"}),
])
@pytest.mark.parametrize("rule", [[], [{"action": "allow", "pattern": "*"}], [{"action": "bypass", "pattern": "*"}]])
def test_doh_shape_is_rejected_on_an_allowed_unlisted_host(addon, tmp_path, path, headers, rule):
    filt = filtering(addon, tmp_path, "filtered", rule)
    control = flow("https://ordinary.example/page")
    filt.request(control)
    assert control.response is None
    f = flow("https://ordinary.example" + path)
    f.request.headers.update(headers)
    if "content-type" in headers:
        f.request.method = "POST"
    filt.request(f)
    assert f.response.status_code == 403
    assert "encrypted DNS" in f.response.content.decode()


def test_proxy_listeners_cannot_be_borrowed_by_another_uid():
    policy = Policy(users=[UserPolicy(uid=1001, username="one", mode="filtered"),
                           UserPolicy(uid=1002, username="two", mode="whitelist")])
    out = nft.render(policy, dns_uid=989, mitm_uid=988, build_gid=300,
                     subids={1001: [(100000, 165535)]})
    assert 'tcp dport 30000 meta skuid != { 0, 1001, 100000-165535 } reject' in out
    assert 'tcp dport 30001 meta skuid != { 0, 1002 } reject' in out
    assert out.index('tcp dport 30001 meta skuid !=') < out.index('oif "lo" accept')
    assert 'tcp dport 29999 meta skgid != 300 meta skuid != 0 reject' in out


def test_lan_guard_precedes_extra_ports_and_high_udp_but_preserves_specific_ssh():
    policy = Policy(users=[UserPolicy(uid=1001, username="kid", mode="filtered",
                                     extra_ports=[8888], ssh_hosts=["192.168.1.2"])])
    out = nft.render(policy, dns_uid=989, mitm_uid=988)
    guard = out.index('meta skuid { 1001 } ip daddr @lan4 reject')
    assert out.index('jump local_services') < guard
    assert out.index('ip daddr @ssh4_1001 tcp dport 22 accept') < guard
    assert guard < out.index('meta skuid 1001 tcp dport { 8888 } accept')
    assert guard < out.index('udp dport >= 1024 accept')


def test_path_only_lan_rule_does_not_open_a_reusable_private_connection(addon, tmp_path):
    filt = filtering(addon, tmp_path, "filtered", [{"action": "allow", "pattern": "router.example/status"}])
    f = flow("http://router.example/status")
    filt.request(f)
    connect(filt, f, ["192.168.1.1"])
    assert f.server_conn.error


def test_a_cached_private_connection_loses_access_when_its_site_permission_is_removed(addon, tmp_path):
    filt = filtering(addon, tmp_path, "filtered", [{"action": "allow", "pattern": "router.example/*"}])
    filt.policy._refresh()
    filt.policy._lan_hosts[1001] = ["router.example"]
    f = flow("http://router.example/status")
    filt.request(f)
    connect(filt, f, ["192.168.1.1"])
    assert f.server_conn.error is None
    f.server_conn.connected = True
    f.server_conn.peername = ("192.168.1.1", 80)
    filt.policy._lan_hosts[1001] = []
    f.response = None
    filt.request(f)
    assert f.response.status_code == 403


def test_category_exempt_payment_names_do_not_grant_private_network_access(addon, tmp_path):
    filt = filtering(addon, tmp_path, "filtered")
    doc = json.loads(filt.policy.path.read_text())
    doc["1001"]["never_blocked"] = ["payment.example"]
    filt.policy.path.write_text(json.dumps(doc))
    f = flow("http://payment.example/")
    filt.request(f)
    connect(filt, f, ["192.168.1.1"])
    assert f.server_conn.error


def test_failed_connection_permission_caches_are_bounded_and_clients_clean_up(addon, tmp_path):
    filt = filtering(addon, tmp_path)
    client = object()
    for i in range(4100):
        server = NS(id=i, connected=False)
        filt._permit_upstream(server, f"h{i}.example", 80, False, client)
    assert len(filt._upstream_permissions) == 4096
    assert len(filt._client_permissions) == 4096
    filt.client_disconnected(client)
    assert not filt._client_permissions


def test_whitelist_names_are_exact_unless_the_parent_added_a_wildcard():
    assert webguard.host_in("approved.example", ["approved.example"])
    assert not webguard.host_in("other.approved.example", ["approved.example"])
    assert webguard.host_in("other.approved.example", ["*.approved.example"])
    assert not webguard.host_in("approved.example.attacker.test", ["*.approved.example"])


def test_ipv6_public_dns_and_lan_multicast_do_not_escape_the_guards():
    policy = Policy(users=[UserPolicy(uid=1001, username="kid", mode="filtered")])
    out = nft.render(policy, dns_uid=989, mitm_uid=988)
    assert "ip6 daddr @doh6 reject" in out
    assert "2606:4700:4700::1111" in out
    assert "ip daddr { 100.64.0.0/10, 224.0.0.0/4, 240.0.0.0/4 } reject" in out
    assert "ip6 daddr { fc00::/7, fe80::/10, ff00::/8 } reject" in out


@pytest.mark.parametrize("entry", ["*.example.org", "https://printer.local", "printer.local/path", "printer.local:8080", "1.1.1.1", "127.0.0.1", "::1", "::ffff:127.0.0.1", "224.0.0.1", "0.0.0.0", "localhost"])
def test_lan_permissions_do_not_accept_wildcards_urls_ports_or_local_daemons(entry):
    with pytest.raises(ValueError):
        webguard.normalize_lan_hosts([entry])


def test_lan_device_permission_roundtrips_for_accounts_and_guest():
    from kosherd.policy import GuestPolicy
    assert webguard.normalize_lan_hosts(["PRINTER.local.", "printer.local", "[fd00::1]"]) == ["printer.local", "fd00::1"]
    policy = Policy(users=[UserPolicy(uid=1001, username="kid", mode="filtered", lan_hosts=["printer.local"])],
                    guest=GuestPolicy(enabled=True, uid=1010, lan_hosts=["192.168.1.10"]))
    loaded = Policy.from_dict(policy.to_dict())
    assert loaded.users[0].lan_hosts == ["printer.local"]
    assert loaded.guest.lan_hosts == ["192.168.1.10"]
    assert loaded.effective_users()[-1].lan_hosts == ["192.168.1.10"]


def test_approved_internet_name_cannot_rebind_to_lan_without_device_permission(addon, tmp_path):
    filt = filtering(addon, tmp_path)
    f = flow("https://approved.example/")
    filt.tls_clienthello(hello(f, "approved.example"))
    connect(filt, f, ["192.168.1.1"])
    assert f.server_conn.error


@pytest.mark.parametrize("address", ["127.0.0.1", "::1", "::ffff:127.0.0.1", "0.0.0.0", "224.0.0.1"])
def test_even_an_explicit_lan_hostname_cannot_rebind_to_a_local_daemon(addon, tmp_path, address):
    filt = filtering(addon, tmp_path, "filtered")
    f = flow("http://printer.example/")
    filt.request(f)
    connect(filt, f, [address])
    assert "local-service" in f.server_conn.error
