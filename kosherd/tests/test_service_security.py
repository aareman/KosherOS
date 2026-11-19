"""Audit N9: remote data is fetched without root, within a narrow route."""

import io
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from kosherd import apply, nft, ytsearch
from kosherd.policy import Policy
from test_mitm_addon import addon, _request_flow, _shop_filter


@pytest.fixture(autouse=True)
def helper_runtime(monkeypatch, tmp_path):
    monkeypatch.setattr(ytsearch, "RUNTIME_DIR", tmp_path)


def test_channel_query_is_data_not_part_of_the_command(monkeypatch):
    query = 'Torah; $(touch /etc/probe)'

    def run(argv, **kw):
        assert query not in " ".join(argv)
        assert json.loads(kw["input"]) == {"query": query}
        assert f"--uid={ytsearch.SERVICE_USER}" in argv
        assert "--property=NoNewPrivileges=yes" in argv
        assert "--property=CapabilityBoundingSet=" in argv
        assert "--property=ProtectSystem=strict" in argv
        assert argv[-5:] == ["/usr/bin/python3", "-I", "-m", "kosherd.ytsearch", "--worker"]
        assert kw["timeout"] == 25
        kw["stdout"].write(b'{"channels":[{"id":"UCabc","title":"Torah"}]}')
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(ytsearch.subprocess, "run", run)
    assert ytsearch.search_channels(query) == [{"id": "UCabc", "title": "Torah"}]


def test_empty_or_oversized_query_never_starts_the_helper(monkeypatch):
    def never(*a, **kw):
        raise AssertionError("helper started")
    monkeypatch.setattr(ytsearch.subprocess, "run", never)
    assert ytsearch.search_channels("  ") == []
    with pytest.raises(ytsearch.SearchError, match="256"):
        ytsearch.search_channels("x" * 257)


@pytest.mark.parametrize("body", [b'{}', b'[]', b'not json', b'{"channels":null}',
    b'{"channels":[{"id":"not-a-channel","title":"x"}]}',
    b'{"channels":[{"id":"UCabc","title":12}]}',
    b'{"channels":[{"id":"UCabc","title":"x","execute":"bad"}]}'])
def test_untrusted_helper_output_is_validated(monkeypatch, body):
    def run(argv, **kw):
        kw["stdout"].write(body)
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(ytsearch.subprocess, "run", run)
    with pytest.raises(ytsearch.SearchError, match="unreadable"):
        ytsearch.search_channels("torah")


def test_helper_failure_is_not_mistaken_for_an_empty_search(monkeypatch):
    def run(argv, **kw):
        kw["stdout"].write(b'{"error":"cannot reach YouTube: offline"}')
        return SimpleNamespace(returncode=1)
    monkeypatch.setattr(ytsearch.subprocess, "run", run)
    with pytest.raises(ytsearch.SearchError, match="offline"):
        ytsearch.search_channels("torah")


def test_helper_wait_is_bounded(monkeypatch):
    def run(argv, **kw):
        raise subprocess.TimeoutExpired(argv, kw["timeout"])
    monkeypatch.setattr(ytsearch.subprocess, "run", run)
    with pytest.raises(ytsearch.SearchError, match="could not finish"):
        ytsearch.search_channels("torah")


def test_large_remote_responses_are_rejected_before_parsing(monkeypatch):
    from test_ytsearch import _Response
    monkeypatch.setattr(ytsearch, "_open", lambda *a, **kw: _Response(b'x' * (ytsearch.MAX_RESPONSE + 1)))
    with pytest.raises(ytsearch.SearchError, match="too much data"):
        ytsearch._fetch_channels("torah")


def test_http_redirects_cannot_change_the_worker_destination():
    assert ytsearch._NoRedirect().redirect_request(None, None, 302, "", {}, "https://example.com") is None


def test_worker_refuses_root_without_fetching(monkeypatch, capsys):
    monkeypatch.setattr(ytsearch.os, "getuid", lambda: 0)
    assert ytsearch.worker() == 1
    assert "must not run as root" in capsys.readouterr().out


def test_worker_fetches_as_its_service_identity(monkeypatch, capsys):
    monkeypatch.setattr(ytsearch.os, "getuid", lambda: 981)
    monkeypatch.setattr(ytsearch.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(b'{"query":"Torah"}')))
    monkeypatch.setattr(ytsearch, "_fetch_channels", lambda q: [{"id": "UCabc", "title": q}])
    assert ytsearch.worker() == 0
    assert json.loads(capsys.readouterr().out) == {"channels": [{"id": "UCabc", "title": "Torah"}]}


def test_channel_uid_cannot_use_other_system_users_network_shortcuts():
    out = nft.render(Policy(), dns_uid=989, mitm_uid=988, channel_search_uid=981)
    output = out.split("chain output {")[1].split("chain input {")[0]
    rules = [s.strip() for s in output.splitlines() if s.strip() and not s.strip().startswith("#")]
    reject = rules.index("meta skuid 981 reject")
    assert reject < rules.index('oif "lo" accept')
    assert reject < rules.index("meta skuid < 1000 accept")
    assert reject < rules.index("ct state established,related accept")
    assert "meta skuid 981 ip daddr 127.0.0.1 tcp dport 29998 accept" in rules[:reject]
    assert "meta skuid 981 ip daddr 127.0.0.1 udp dport 53 accept" in rules[:reject]
    assert "meta skuid 981 tcp dport 443 redirect to :29998" in out


def test_other_accounts_cannot_borrow_the_channel_search_listener():
    out = nft.render(Policy(), dns_uid=989, mitm_uid=988, channel_search_uid=981)
    guard = f"ip daddr 127.0.0.0/8 tcp dport {nft.CHANNEL_SEARCH_PORT} meta skuid != {{ 0, 981 }} reject"
    assert out.index(guard) < out.index('oif "lo" accept')


def test_proxy_keeps_the_channel_identity_without_any_filtered_accounts(monkeypatch, tmp_path, addon):
    monkeypatch.setattr(apply, "channel_search_uid", lambda: 981)
    monkeypatch.setattr(apply.nixdaemon, "build_gid", lambda: None)
    monkeypatch.setattr(apply, "MITM_DIR", tmp_path)
    monkeypatch.setattr(apply, "MITM_RULES_PATH", tmp_path / "rules.json")
    apply.write_mitm_rules(Policy(), ())
    doc = json.loads((tmp_path / "rules.json").read_text())
    assert doc["channel-search"]["mode"] == "filtered"
    assert "transparent@127.0.0.1:29998" in (tmp_path / "listen.env").read_text()
    assert addon.PolicyCache(tmp_path / "rules.json").uid_for_listener_port(29998) == addon.CHANNEL_SEARCH_UID


@pytest.mark.parametrize("url,method,allowed", [
    ("https://www.youtube.com/youtubei/v1/search?prettyPrint=false", "POST", True),
    ("https://www.youtube.com/youtubei/v1/search", "GET", False),
    ("https://www.youtube.com/watch?v=anything", "POST", False),
    ("https://www.youtube.com/youtubei/v1/browse", "POST", False),
    ("https://youtube.com/youtubei/v1/search", "POST", False),
    ("https://www.youtube.com.attacker.invalid/youtubei/v1/search", "POST", False),
    ("https://www.youtube.com:444/youtubei/v1/search", "POST", False),
    ("https://www.youtube.com:bad/youtubei/v1/search", "POST", False),
    ("http://www.youtube.com/youtubei/v1/search", "POST", False),
    ("https://user@www.youtube.com/youtubei/v1/search", "POST", False),
])
def test_channel_proxy_allows_only_the_fixed_endpoint(addon, url, method, allowed):
    filt = _shop_filter(addon)
    filt._uid_of = lambda flow: addon.CHANNEL_SEARCH_UID
    flow = _request_flow(url)
    flow.request.method = method
    filt.request(flow)
    assert (flow.response is None) == allowed
    if not allowed:
        assert flow.response.status_code == 403


def test_nix_mount_has_valid_mount_controls_not_service_properties():
    unit = (Path(__file__).parents[2] / "os-image/files/usr/lib/systemd/system/nix.mount").read_text()
    assert "Options=bind,nosuid,nodev" in unit
    assert "ProtectSystem=" not in unit


def test_nix_cannot_silently_fall_back_to_an_unsandboxed_build():
    root = Path(__file__).parents[2]
    conf = (root / "os-image/files/etc/nix/nix.conf").read_text()
    assert "sandbox-fallback = false" in conf
    unit = (root / "os-image/files/usr/lib/systemd/system/nix-daemon.service.d/20-kosher-hardening.conf").read_text()
    settings = [line for line in unit.splitlines() if not line.startswith('#')]
    assert "ProtectKernelTunables=yes" not in settings
    assert "ProtectKernelLogs=yes" not in settings
