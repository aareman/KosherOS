"""Checked streaming through a real loopback mitmdump process.

What a unit test of the callback cannot show: that mitmproxy actually
delivers the checked prefix before the origin finishes, that a refusal
forwards nothing, and that a script on the AI site arrives untouched.
"""

import gzip
import json
import os
import re
import selectors
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(shutil.which("mitmdump") is None or shutil.which("curl") is None,
                                reason="mitmdump and curl are required")

ADDON = '''
from kosherd.ai_proxy import AIFilter
from kosherd import content, language, vision
class Policy:
    def media_level_for(self, uid): return "all"
    def language_filter_for(self, uid): return "substitute"
    def blocked_categories_for(self, uid): return []
class Owner:
    policy = Policy()
    wordlist = language.Wordlist({"damn": "darn"})
    scorer = content.Scorer()
    vision = vision.ImageFilter()
class Account:
    """What the main addon does: name the filtered account on every flow."""
    def request(self, flow): flow.metadata["kosher_uid"] = 1000
addons = [Account(), AIFilter(Owner())]
'''

SCRIPT = b"window.__sentinel = 1; // a damn useful script\n"


def _proxy_port(proxy: subprocess.Popen) -> int:
    assert proxy.stdout is not None
    with selectors.DefaultSelector() as ready:
        ready.register(proxy.stdout, selectors.EVENT_READ)
        startup = b""
        for _ in range(20):
            assert ready.select(10), startup.decode()
            startup += os.read(proxy.stdout.fileno(), 65536)
            match = re.search(rb"listening at 127\.0\.0\.1:(\d+)", startup)
            if match:
                return int(match.group(1))
    raise AssertionError(startup.decode())


@pytest.mark.parametrize("malformed, compressed", [(False, False), (True, False), (False, True)])
def test_real_proxy_checks_stream_before_upstream_finishes(tmp_path: Path, malformed: bool,
                                                           compressed: bool) -> None:
    # Given a loopback origin whose completion is controlled by this client.
    finish = threading.Event()

    class Origin(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path.endswith("sdk.js"):
                self.send_response(200)
                self.send_header("Content-Type", "text/javascript; charset=utf-8")
                self.send_header("Content-Length", str(len(SCRIPT)))
                self.end_headers()
                self.wfile.write(SCRIPT)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            if compressed:
                self.send_header("Content-Encoding", "gzip")
            self.end_headers()
            payload = {"message": {"status": "in_progress", "content": {
                "parts": ["damn useful words. " * 80], "content_type": "text",
            }}}
            first = b"data: {invalid}\n\n" if malformed else f"data: {json.dumps(payload)}\n\n".encode()
            packer = gzip.GzipFile(fileobj=self.wfile, mode="wb") if compressed else None
            (packer or self.wfile).write(first)
            if packer:
                packer.flush()
            self.wfile.flush()
            if finish.wait(15):
                payload["message"]["status"] = "finished_successfully"
                tail = f"data: {json.dumps(payload)}\n\ndata: [DONE]\n\n".encode()
                (packer or self.wfile).write(tail)
                if packer:
                    packer.close()
                self.wfile.flush()

        def log_message(self, format: str, *args: str) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Origin)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    addon = tmp_path / "addon.py"
    addon.write_text(ADDON)
    command = ["mitmdump", "--mode", f"reverse:http://127.0.0.1:{server.server_port}",
               "--listen-host", "127.0.0.1", "--listen-port", "0", "--set", "keep_host_header=true",
               "--set", f"confdir={tmp_path / 'ca'}", "-s", str(addon)]
    try:
        with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              env=os.environ | {"PYTHONUNBUFFERED": "1"}) as proxy:
            try:
                port = _proxy_port(proxy)
                # The anti-bot script on the same site is not an answer: it arrives as sent.
                script = subprocess.run([
                    "curl", "--silent", "--show-error", "--max-time", "10", "-i",
                    "-H", "Host: chatgpt.com", f"http://127.0.0.1:{port}/backend-api/sentinel/sdk.js",
                ], capture_output=True, check=True).stdout
                assert script.split(b"\r\n", 1)[0].endswith(b" 200 OK")
                assert script.endswith(SCRIPT)
                with subprocess.Popen([
                    "curl", "--silent", "--show-error", "--no-buffer", "--max-time", "15",
                    "-H", "Host: chatgpt.com", f"http://127.0.0.1:{port}/backend-api/conversation",
                ], stdout=subprocess.PIPE, stderr=subprocess.PIPE) as client:
                    assert client.stdout is not None
                    try:
                        # When the first checked event is available before origin completion.
                        with selectors.DefaultSelector() as readable:
                            readable.register(client.stdout, selectors.EVENT_READ)
                            assert readable.select(10), "proxy buffered the entire generation"
                            early = os.read(client.stdout.fileno(), 65536)
                        # Then the wire already carries filtered text, with no raw word leak.
                        expected = b"kosher_content_blocked" if malformed else b"darn useful"
                        assert expected in early
                        assert b"damn" not in early
                        assert not finish.is_set()
                        finish.set()
                        rest, errors = client.communicate(timeout=10)
                        assert client.returncode == 0, errors.decode()
                        assert b"damn" not in early + rest
                        assert (b"[DONE]" in rest) is not malformed
                    finally:
                        finish.set()
                        if client.poll() is None:
                            client.terminate()
                            client.wait(timeout=5)
            finally:
                proxy.terminate()
                proxy.wait(timeout=5)
    finally:
        finish.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
