"""The AWS sign-in workaround changes only its upstream HTTP transport."""

from types import SimpleNamespace

import pytest

from test_mitm_addon import addon as addon


def tls_data(host="us-east-1.signin.aws", *, prepared=False):
    server = SimpleNamespace(sni=host, address=("192.0.2.1", 443),
                             alpn_offers=(b"h2", b"http/1.1"))
    client = SimpleNamespace(sni="us-east-1.signin.aws", alpn_offers=(b"h2", b"http/1.1"))
    calls = []
    ssl_conn = SimpleNamespace(set_alpn_protos=calls.append) if prepared else None
    return SimpleNamespace(conn=server, context=SimpleNamespace(server=server, client=client),
                           is_dtls=False, ssl_conn=ssl_conn), calls


@pytest.mark.parametrize("prepared", [False, True])
@pytest.mark.parametrize("host", ["us-east-1.signin.aws", "US-EAST-1.SIGNIN.AWS."])
def test_signin_uses_http1_before_or_after_builtin_tls_configuration(addon, prepared, host):
    data, calls = tls_data(host, prepared=prepared)
    original_ssl = data.ssl_conn
    proxy = addon.KosherFilter.__new__(addon.KosherFilter)
    proxy.tls_start_server(data)
    assert data.conn.alpn_offers == (b"http/1.1",)
    assert calls == ([[b"http/1.1"]] if prepared else [])
    assert data.ssl_conn is original_ssl, "keep the TLS configuration and verification context"
    assert data.context.client.alpn_offers == (b"h2", b"http/1.1")
    assert data.conn.sni == host


@pytest.mark.parametrize("host", [
    "example.com", "signin.aws", "us-west-2.signin.aws",
    "us-east-1.signin.aws.example.com", "not-us-east-1.signin.aws",
    "signin.aws.amazon.com",
])
def test_other_hosts_keep_http2_even_when_client_sni_matches(addon, host):
    data, calls = tls_data(host, prepared=True)
    addon.KosherFilter.tls_start_server(None, data)
    assert data.conn.alpn_offers == (b"h2", b"http/1.1")
    assert calls == []


@pytest.mark.parametrize("client_sni", ["us-east-1.signin.aws", None])
def test_unconfigured_server_uses_the_same_sni_fallback_as_tlsconfig(addon, client_sni):
    data, _ = tls_data(None)
    data.context.client.sni = client_sni
    data.conn.address = ("us-east-1.signin.aws", 443)
    addon.KosherFilter.tls_start_server(None, data)
    assert data.conn.alpn_offers == (b"http/1.1",)


@pytest.mark.parametrize("kind", ["dtls", "upstream_proxy"])
def test_only_origin_tls_is_changed(addon, kind):
    data, calls = tls_data(prepared=True)
    if kind == "dtls":
        data.is_dtls = True
    else:
        data.context.server = SimpleNamespace(sni="us-east-1.signin.aws")
    addon.KosherFilter.tls_start_server(None, data)
    assert data.conn.alpn_offers == (b"h2", b"http/1.1")
    assert calls == []
