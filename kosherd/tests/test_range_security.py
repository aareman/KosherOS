"""Video sampling cannot bypass the proxy's approved upstream connection."""
import io
import ssl
from types import SimpleNamespace

import pytest

from kosherd import webguard


@pytest.fixture
def wire(monkeypatch):
    class Wire:
        sent = b''
        closed = False
        status = 206
        body = b'piece'
        connections = []
        names = []
        def sendall(self, data):
            self.sent += data
        def makefile(self, *args):
            return io.BytesIO(f'HTTP/1.1 {self.status} Test\r\nContent-Length: {len(self.body)}\r\nLocation: http://127.0.0.1/private\r\n\r\n'.encode() + self.body)
        def close(self):
            self.closed = True
    value = Wire()
    def connect(address, timeout):
        value.connections.append(address)
        return value
    def wrap(sock, *, server_hostname):
        value.names.append(server_hostname)
        return sock
    monkeypatch.setattr(webguard.socket, 'create_connection', connect)
    monkeypatch.setattr(ssl, 'create_default_context', lambda: SimpleNamespace(wrap_socket=wrap))
    return value


def test_pinned_https_keeps_hostname_verification_and_ignores_proxy_environment(wire, monkeypatch):
    monkeypatch.setenv('https_proxy', 'http://127.0.0.1:8000')
    assert webguard.fetch_range('https://approved.example/movie?x=1', ('93.184.216.34', 443), {}, 10, 19) == b'piece'
    assert wire.connections == [('93.184.216.34', 443)]
    assert wire.names == ['approved.example']
    assert b'Host: approved.example\r\n' in wire.sent
    assert b'Range: bytes=10-19\r\n' in wire.sent
    assert b'GET /movie?x=1 HTTP/1.1' in wire.sent
    assert wire.closed


@pytest.mark.parametrize('status', [200, 301, 302, 307, 308])
def test_redirects_and_whole_downloads_are_refused(wire, status):
    wire.status = status
    with pytest.raises(OSError, match='HTTP'):
        webguard.fetch_range('http://approved.example/movie', ('93.184.216.34', 80), {}, 0, 9)
    assert wire.connections == [('93.184.216.34', 80)]
    assert wire.closed


@pytest.mark.parametrize('peer,lan', [(None, False), (('hostname.example', 80), False),
    (('127.0.0.1', 80), True), (('::1', 80), True), (('10.0.0.2', 80), False),
    (('fd33::2', 80), False), (('93.184.216.34', 1234), False)])
def test_missing_local_or_unapproved_private_peers_never_connect(wire, peer, lan):
    with pytest.raises(OSError):
        webguard.fetch_range('http://approved.example/movie', peer, {}, 0, 9, lan=lan)
    assert not wire.connections


@pytest.mark.parametrize('address', ['10.0.0.2', 'fd33::2', '2001:4860:4860::8888'])
def test_explicit_private_device_and_public_ipv6_keep_their_pinned_peer(wire, address):
    assert webguard.fetch_range('http://approved.example/movie', (address, 80), {}, 0, 9, lan=True) == b'piece'
    assert wire.connections == [(address, 80)]


def test_an_oversized_partial_response_is_bounded_and_refused(wire):
    wire.body = b'x' * 100
    with pytest.raises(OSError, match='exceeded'):
        webguard.fetch_range('http://approved.example/movie', ('93.184.216.34', 80), {}, 0, 9)
    assert wire.closed
