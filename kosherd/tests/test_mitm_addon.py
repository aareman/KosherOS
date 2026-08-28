"""Tests for the mitmproxy addon's uid lookup and policy cache.

The addon itself imports mitmproxy, which the dev shell doesn't have, so we
test the two pieces that carry the real risk — parsing /proc/net/tcp and
reloading the policy — by loading the module with a stub in place.
"""

import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest

ADDON = Path(__file__).parents[2] / "mitm" / "kosher_filter.py"


@pytest.fixture
def addon(monkeypatch):
    """Import kosher_filter with mitmproxy stubbed out."""
    mitmproxy = types.ModuleType("mitmproxy")
    http_mod = types.ModuleType("mitmproxy.http")
    http_mod.HTTPFlow = object
    http_mod.Response = types.SimpleNamespace(make=lambda *a, **k: ("response", a))
    mitmproxy.http = http_mod
    monkeypatch.setitem(sys.modules, "mitmproxy", mitmproxy)
    monkeypatch.setitem(sys.modules, "mitmproxy.http", http_mod)

    spec = importlib.util.spec_from_file_location("kosher_filter_under_test", ADDON)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_uid_lookup_reads_proc_net_tcp(addon, tmp_path):
    # Two sockets; the one on port 0xC000 (49152) belongs to uid 1001.
    proc = tmp_path / "tcp"
    proc.write_text(
        "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n"
        "   0: 0100007F:1F90 00000000:0000 0A 00000000:00000000 00:00000000 00000000  989        0 1\n"
        "   1: 0100007F:C000 5DB8D822:01BB 01 00000000:00000000 00:00000000 00000000 1001        0 2\n"
    )
    lookup = addon.UidLookup()
    lookup.PATHS = (str(proc),)
    assert lookup.uid_for_port(49152) == 1001
    assert lookup.uid_for_port(8080) == 989
    assert lookup.uid_for_port(12345) is None


def test_uid_lookup_survives_missing_files(addon):
    lookup = addon.UidLookup()
    lookup.PATHS = ("/nonexistent/tcp",)
    assert lookup.uid_for_port(1234) is None


def _policy(tmp_path, users, guest=None):
    doc = {"schema_version": 1, "revision": 1, "source": "local",
           "users": users, "guardian": {"enabled": False}}
    if guest:
        doc["guest"] = guest
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(doc))
    return path


def test_policy_cache_loads_rules_per_uid(addon, tmp_path):
    path = _policy(tmp_path, [
        {"uid": 1001, "username": "kid", "mode": "inspect",
         "rules": [{"action": "block", "pattern": "youtube.com/watch*"}]},
        {"uid": 1002, "username": "other", "mode": "dnsfilter"},
    ])
    cache = addon.PolicyCache(path)
    assert len(cache.rules_for(1001)) == 1
    assert cache.rules_for(1002) == []
    assert cache.rules_for(None) == []
    assert cache.rules_for(4242) == []


def test_policy_cache_includes_enabled_guest(addon, tmp_path):
    path = _policy(tmp_path, [], guest={
        "enabled": True, "uid": 1010, "mode": "inspect",
        "rules": [{"action": "block", "pattern": "*"}]})
    assert len(addon.PolicyCache(path).rules_for(1010)) == 1


def test_policy_cache_reloads_on_change(addon, tmp_path):
    path = _policy(tmp_path, [{"uid": 1001, "username": "k", "mode": "inspect",
                               "rules": []}])
    cache = addon.PolicyCache(path)
    assert cache.rules_for(1001) == []
    import os
    import time
    doc = json.loads(path.read_text())
    doc["users"][0]["rules"] = [{"action": "block", "pattern": "bad.com"}]
    path.write_text(json.dumps(doc))
    os.utime(path, (time.time() + 10, time.time() + 10))  # ensure mtime differs
    assert len(cache.rules_for(1001)) == 1


def test_policy_cache_tolerates_bad_rules(addon, tmp_path):
    path = _policy(tmp_path, [{"uid": 1001, "username": "k", "mode": "inspect",
                               "rules": [{"action": "explode", "pattern": "x"}]}])
    assert addon.PolicyCache(path).rules_for(1001) == []


def test_policy_cache_tolerates_missing_file(addon, tmp_path):
    assert addon.PolicyCache(tmp_path / "nope.json").rules_for(1001) == []
