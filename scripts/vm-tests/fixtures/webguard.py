#!/usr/bin/env python3
"""Offline acceptance checks of the installed proxy's hostname boundary.

A real mitmdump and local HTTP/TLS origins; both hostnames resolve to the
same address. Only the resolver answers/category fixture are substituted.
The request/SNI hooks, DNS pinning and socket forwarding are production code.
This runs unprivileged as well as in 80-hardening on a development VM.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import http.client
import json
import os
from pathlib import Path
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID


IPV6 = os.environ.get("KOSHER_TEST_IPV6") == "1"
LISTENER = "::1" if IPV6 else "127.0.0.1"
UPSTREAM = "fd33:abcd:1234::1" if IPV6 else "10.254.253.1"
URL_HOST = f"[{UPSTREAM}]" if IPV6 else UPSTREAM

class OriginServer(ThreadingHTTPServer):
    address_family = socket.AF_INET6 if IPV6 else socket.AF_INET


class Origin(BaseHTTPRequestHandler):
    seen = 0

    def answer(self):
        self.rfile.read(int(self.headers.get("Content-Length", "0")))
        type(self).seen += 1
        if self.path == "/range-redirect":
            self.send_response(302)
            self.send_header("Location", "http://127.0.0.1:9/private")
            self.end_headers()
            return
        body = b"controlled origin answered"
        partial = self.path == "/range"
        if partial:
            first, last = map(int, self.headers["Range"].removeprefix("bytes=").split("-"))
            body = body[first:last + 1]
        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = answer
    do_POST = answer

    def log_message(self, *_args):
        pass


def free_ports(count):
    # Keep all reservations open until every port has been selected. In
    # a fresh network namespace, repeated bind(0)/close can reuse the
    # same ephemeral port immediately.
    sockets = []
    try:
        for _ in range(count):
            sock = socket.socket()
            sockets.append(sock)
            sock.bind(("127.0.0.1", 0))
        return [sock.getsockname()[1] for sock in sockets]
    finally:
        for sock in sockets:
            sock.close()


def certificate(directory):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "approved.example")])
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(datetime.now(timezone.utc) - timedelta(days=1))
            .not_valid_after(datetime.now(timezone.utc) + timedelta(days=1))
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), True)
            .add_extension(x509.SubjectAlternativeName([x509.DNSName("approved.example")]), False)
            .sign(key, hashes.SHA256()))
    (directory / "cert.pem").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    (directory / "key.pem").write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    return cert.public_bytes(serialization.Encoding.DER)


def http_probe(port, host, path="/", headers=None, method="GET"):
    conn = http.client.HTTPConnection(LISTENER, port, timeout=5)
    try:
        conn.request(method, path, headers={"Host": host, **(headers or {})})
        response = conn.getresponse()
        return response.status, response.read()
    finally:
        conn.close()


def tls_probe(port, host, expected_cert, intercepted=False):
    # The origin certificate is deliberately self-signed; checking its
    # exact DER proves that the proxy did not terminate this TLS session.
    context = ssl._create_unverified_context()
    with context.wrap_socket(socket.create_connection((LISTENER, port), timeout=5),
                             server_hostname=host) as conn:
        same = conn.getpeercert(binary_form=True) == expected_cert
        assert same != intercepted, "unexpected TLS interception mode"
        conn.sendall(b"GET / HTTP/1.1\r\nHost: approved.example\r\nConnection: close\r\n\r\n")
        response = http.client.HTTPResponse(conn)
        response.begin()
        assert response.status == 200
        assert response.read() == b"controlled origin answered"


def ech_probe(port):
    # Add an ECH offer to a real OpenSSL ClientHello. ECH and GREASE cannot
    # safely be distinguished by the intermediary, so both must be refused.
    incoming, outgoing = ssl.MemoryBIO(), ssl.MemoryBIO()
    connection = ssl._create_unverified_context().wrap_bio(
        incoming, outgoing, server_side=False, server_hostname="approved.example")
    try:
        connection.do_handshake()
    except ssl.SSLWantReadError:
        pass
    record = bytearray(outgoing.read())
    pos = 5 + 4 + 2 + 32
    pos += 1 + record[pos]
    pos += 2 + int.from_bytes(record[pos:pos + 2], "big")
    pos += 1 + record[pos]
    extension = b"\xfe\x0d\x00\x01\x00"
    record[pos:pos + 2] = (int.from_bytes(record[pos:pos + 2], "big") + len(extension)).to_bytes(2, "big")
    record += extension
    record[3:5] = (len(record) - 5).to_bytes(2, "big")
    record[6:9] = (len(record) - 9).to_bytes(3, "big")
    with socket.create_connection((LISTENER, port), timeout=5) as conn:
        conn.sendall(record)
        assert not conn.recv(4096), "ECH offer was not refused before the handshake"


def check():
    executable = shutil.which("mitmdump")
    if not executable:
        raise RuntimeError("mitmdump is required for the hostname acceptance check")
    repo = Path(__file__).resolve().parents[3]
    addon = repo / "mitm/kosher_filter.py"
    if not addon.exists():
        addon = Path("/usr/share/kosher/mitm/kosher_filter.py")
    with tempfile.TemporaryDirectory(prefix="kosher-webguard-") as folder:
        directory = Path(folder)
        der = certificate(directory)
        http_origin = OriginServer(("::" if IPV6 else "0.0.0.0", 0), Origin)
        tls_origin = OriginServer(("::" if IPV6 else "0.0.0.0", 0), Origin)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(str(directory / "cert.pem"), str(directory / "key.pem"))
        received_sni = []
        context.set_servername_callback(lambda sock, name, ctx: received_sni.append(name))
        tls_origin.socket = context.wrap_socket(tls_origin.socket, server_side=True)
        for server in (http_origin, tls_origin):
            threading.Thread(target=server.serve_forever, daemon=True).start()
        ports = free_ports(5)
        filtered, wh_http, wh_tls, basic_tls, inspected_tls = ports
        policy = {
            "1001": {"port": filtered, "mode": "filtered", "rules": [
                {"action": "allow", "pattern": "ordinary.example/*"},
                {"action": "bypass", "pattern": "bypass.example/*"},
                {"action": "allow", "pattern": "path-only.example/safe"}],
                "media_level": "none", "blocked_categories": [],
                "lan_hosts": ["ordinary.example", "bypass.example", "loopback.example"]},
            "1002": {"port": wh_http, "mode": "whitelist", "allowed_hosts": ["approved.example"],
                     "lan_hosts": ["approved.example"]},
            "1003": {"port": wh_tls, "mode": "whitelist", "allowed_hosts": ["approved.example"],
                     "lan_hosts": ["approved.example"]},
            "1005": {"port": inspected_tls, "mode": "filtered", "rules": [
                {"action": "allow", "pattern": "approved.example/*"}], "media_level": "none", "lan_hosts": ["approved.example"]},
            "1004": {"port": basic_tls, "mode": "dnsfilter", "blocked_categories": ["adult"],
                     "lan_hosts": ["approved.example"]},
        }
        (directory / "policy.json").write_text(json.dumps(policy))
        # Only DNS/category inputs are controlled. Every decision and the
        # real mitmproxy connection lifecycle are left to the shipped addon.
        wrapper = f'''import asyncio, importlib.util, socket
from pathlib import Path
spec = importlib.util.spec_from_file_location("tested_filter", {str(addon)!r})
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
class Checked(m.KosherFilter):
    async def server_connect(self, data):
        loop = asyncio.get_running_loop()
        original = loop.getaddrinfo
        async def resolve(host, port, **kw):
            if host.endswith(".example"):
                addresses = ["93.184.216.34", "{UPSTREAM}"] if host == "mixed.example" else ["{UPSTREAM}"]
                if host == "loopback.example": addresses = ["127.0.0.1"]
                return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (a, port)) for a in addresses]
            return await original(host, port, **kw)
        loop.getaddrinfo = resolve
        try:
            await super().server_connect(data)
        finally:
            loop.getaddrinfo = original
class Categories:
    def blocked_categories_of(self, host, blocked):
        return {{"adult"}} if host == "blocked.example" and "adult" in blocked else set()
f = Checked()
f.policy = m.PolicyCache(Path({str(directory / 'policy.json')!r}))
f.categories = Categories()
addons = [f]
'''
        (directory / "addon.py").write_text(wrapper)
        env = dict(os.environ)
        if (repo / "kosherd/src").exists():
            env["PYTHONPATH"] = str(repo / "kosherd/src") + os.pathsep + env.get("PYTHONPATH", "")
        argv = [executable, "--set", "connection_strategy=lazy", "--set", "rawtcp=false",
                "--set", "upstream_cert=false", "--set", "keep_host_header=true",
                "--set", "ssl_verify_upstream_trusted_ca=" + str(directory / "cert.pem"),
                "--set", "confdir=" + str(directory / "ca"), "-s", str(directory / "addon.py")]
        for port in (filtered, wh_http):
            argv += ["--mode", f"reverse:http://{URL_HOST}:{http_origin.server_port}@{LISTENER}:{port}"]
        for port in (wh_tls, basic_tls, inspected_tls):
            argv += ["--mode", f"reverse:https://{URL_HOST}:{tls_origin.server_port}@{LISTENER}:{port}"]
        with (directory / "proxy.log").open("w+") as log:
            process = subprocess.Popen(argv, env=env, stdout=log, stderr=log)
            try:
                for _ in range(100):
                    if process.poll() is not None:
                        raise RuntimeError("proxy did not start")
                    try:
                        with socket.create_connection((LISTENER, wh_tls), timeout=.1):
                            break
                    except OSError:
                        time.sleep(.1)
                else:
                    raise RuntimeError("proxy did not listen")
                assert http_probe(filtered, "ordinary.example")[0] == 200
                for host in ("ordinary.example", "bypass.example"):
                    for path, headers in (("/dns-query?dns=AAAB", {}),
                                          ("/resolve", {"Content-Type": "application/dns-message"}),
                                          ("/resolve", {"Accept": "application/dns-message"})):
                        before = Origin.seen
                        status, body = http_probe(filtered, host, path, headers, "POST")
                        assert status == 403 and b"encrypted DNS" in body, (status, body)
                        assert Origin.seen == before, "DoH request reached the origin"
                print("PASS [F4] DoH shapes refused before forwarding, with allow/bypass controls")
                assert http_probe(wh_http, "approved.example")[0] == 200
                for host in ("refused.example", "127.0.0.1"):
                    before = Origin.seen
                    assert http_probe(wh_http, host)[0] == 403
                    assert Origin.seen == before
                tls_probe(wh_tls, "approved.example", der)
                for host in ("refused.example", None):
                    before = Origin.seen
                    try:
                        tls_probe(wh_tls, host, der)
                    except (ssl.SSLError, ConnectionError):
                        pass
                    else:
                        raise AssertionError(f"TLS admitted {host!r}")
                    assert Origin.seen == before
                ech_probe(wh_tls)
                tls_probe(wh_tls, "approved.example", der)
                tls_probe(basic_tls, "approved.example", der)
                try:
                    tls_probe(basic_tls, "blocked.example", der)
                except (ssl.SSLError, ConnectionError):
                    pass
                else:
                    raise AssertionError("basic protection admitted a blocked TLS name")
                print("PASS [F2/F5] Host and SNI checked on a shared address; TLS certificate unchanged; no SNI/ECH refused")
                for host in ("router.example", "mixed.example", "path-only.example"):
                    before = Origin.seen
                    status, body = http_probe(filtered, host, "/safe")
                    assert status == 502 and b"private-network" in body, (status, body)
                    assert Origin.seen == before
                status, body = http_probe(filtered, "loopback.example")
                assert status == 502 and b"local-service" in body, (status, body)
                assert http_probe(filtered, "ordinary.example")[0] == 200
                tls_probe(inspected_tls, "approved.example", der, intercepted=True)
                assert received_sni[-1] == "approved.example", received_sni
                print("PASS [F6] private/mixed DNS and path-only LAN exception refused; explicit LAN HTTP/HTTPS works")
                # The range sampler uses real HTTP/TLS too, with the peer
                # already checked by the proxy. No DNS lookup of this fake
                # hostname is available to hide a failure to pin its address.
                from unittest.mock import patch
                if (repo / "kosherd/src").exists():
                    sys.path.insert(0, str(repo / "kosherd/src"))
                from kosherd import webguard
                with patch.dict(os.environ, {"SSL_CERT_FILE": str(directory / "cert.pem")}):
                    for scheme, origin in (("http", http_origin), ("https", tls_origin)):
                        url = f"{scheme}://approved.example:{origin.server_port}"
                        peer = (UPSTREAM, origin.server_port)
                        assert webguard.fetch_range(url + "/range", peer, {}, 0, 7, lan=True) == b"controll"
                        try:
                            webguard.fetch_range(url + "/range-redirect", peer, {}, 0, 7, lan=True)
                        except OSError as error:
                            assert "HTTP 302" in str(error), str(error)
                        else:
                            raise AssertionError("the range fetcher followed a redirect")
                print("PASS [F6] video ranges use the pinned peer, verify TLS hostname, and refuse redirects")
            except BaseException:
                process.terminate()
                process.wait(timeout=5)
                log.seek(0)
                print(log.read())
                raise
            finally:
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=5)
                for server in (http_origin, tls_origin):
                    server.shutdown()
                    server.server_close()


if __name__ == "__main__":
    if "--isolated" not in sys.argv:
        # The local origin gets a real private address inside a disposable
        # namespace. Loopback is never a grantable upstream, even in tests.
        command = ["unshare", "--net"]
        if os.geteuid() != 0:
            command += ["--user", "--map-root-user"]
        subprocess.run([*command, sys.executable, str(Path(__file__).resolve()), "--isolated"], check=True)
    else:
        subprocess.run(["ip", "link", "set", "lo", "up"], check=True)
        subprocess.run(["ip", "addr", "add", UPSTREAM + ("/128" if IPV6 else "/32"), "dev", "lo"], check=True)
        check()
