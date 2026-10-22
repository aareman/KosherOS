"""A Nix build downloads only from known source hosts (#199).

Once the build users could resolve names (#190), a fixed-output build
fetched through the builders' proxy listener as a supervised identity
judged by the default categories alone, and example.com is in none of
them. Now the builders' entry in the proxy's rules file carries the hosts
a build may fetch from, and the proxy refuses the rest in plain text that
the build log shows.
"""

import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest

from kosherd import access, apply, buildsources, nft, nixdaemon
from kosherd.daemon import Daemon
from kosherd.policy import Policy, PolicyError, UserPolicy

ADDON = Path(__file__).parents[2] / "mitm" / "kosher_filter.py"


def _policy(**kw) -> Policy:
    return Policy(revision=1, users=[UserPolicy(uid=1001, username="kid", mode="filtered")], **kw)


# ---- the list and how it matches ----------------------------------------------

@pytest.mark.parametrize("host", [
    "github.com", "codeload.github.com", "objects.githubusercontent.com",
    "raw.githubusercontent.com", "release-assets.githubusercontent.com",
    "gitlab.com", "codeberg.org", "bitbucket.org",
    "downloads.sourceforge.net", "netcologne.dl.sourceforge.net",
    "ftp.gnu.org", "ftpmirror.gnu.org", "download.savannah.gnu.org",
    "cdn.kernel.org", "mirrors.kernel.org", "pypi.org", "files.pythonhosted.org",
    "crates.io", "static.crates.io", "index.crates.io", "registry.npmjs.org",
    "registry.yarnpkg.com", "proxy.golang.org", "sum.golang.org", "rubygems.org",
    "hackage.haskell.org", "repo1.maven.org", "cache.nixos.org",
    "channels.nixos.org", "tarballs.nixos.org", "releases.nixos.org",
    "GitHub.com.",
])
def test_the_known_source_hosts_are_allowed(host):
    assert buildsources.allows(host, buildsources.DEFAULT_BUILD_SOURCES)


@pytest.mark.parametrize("host", [
    "example.com", "evilgithub.com", "github.com.example.com",
    "githubusercontent.com.example.com", "notpypi.org", "", None, "93.184.215.14",
])
def test_anything_else_is_refused(host):
    assert not buildsources.allows(host, buildsources.DEFAULT_BUILD_SOURCES)


def test_every_built_in_entry_is_a_policy_domain():
    for entry in buildsources.DEFAULT_BUILD_SOURCES:
        assert buildsources.normalize(entry) == entry


def test_an_entry_is_stored_as_a_bare_host():
    assert buildsources.normalize(" https://Mirror.Example.org/pub/ ") == "mirror.example.org"
    assert buildsources.normalize("*.example.org") == "*.example.org"
    for bad in ("", "not a host", "localhost", "1.2.3.4", "*.", "a..b"):
        with pytest.raises(ValueError):
            buildsources.normalize(bad)


# ---- the policy ---------------------------------------------------------------

def test_no_additions_writes_nothing_and_reads_back_empty():
    doc = _policy().to_dict()
    assert "developer" not in doc
    assert Policy.from_dict(doc).build_sources_extra == []


def test_additions_round_trip():
    doc = _policy(build_sources_extra=["mirror.example.org", "*.example.net"]).to_dict()
    assert doc["developer"] == {"build_sources_extra": ["mirror.example.org", "*.example.net"]}
    again = Policy.from_dict(json.loads(json.dumps(doc)))
    assert again.build_sources_extra == ["mirror.example.org", "*.example.net"]
    sources = again.build_sources()
    assert sources[:len(buildsources.DEFAULT_BUILD_SOURCES)] == list(buildsources.DEFAULT_BUILD_SOURCES)
    assert "mirror.example.org" in sources and "*.example.net" in sources


def test_the_schema_refuses_something_that_is_not_a_host():
    doc = _policy().to_dict()
    doc["developer"] = {"build_sources_extra": ["https://example.org/x"]}
    with pytest.raises(PolicyError):
        Policy.from_dict(doc)


def test_the_proxy_rules_carry_the_hosts(monkeypatch, tmp_path):
    monkeypatch.setattr(apply, "MITM_DIR", tmp_path)
    monkeypatch.setattr(apply, "MITM_RULES_PATH", tmp_path / "rules.json")
    monkeypatch.setattr(nixdaemon, "build_gid", lambda: 999)
    apply.write_mitm_rules(_policy(build_sources_extra=["mirror.example.org"]))
    builder = json.loads((tmp_path / "rules.json").read_text())["builder"]
    assert builder["port"] == nft.BUILDER_PORT
    assert "github.com" in builder["allowed_hosts"]
    assert "mirror.example.org" in builder["allowed_hosts"]
    assert "example.com" not in builder["allowed_hosts"]


# ---- the daemon call ----------------------------------------------------------

def _daemon(monkeypatch):
    daemon = Daemon.__new__(Daemon)
    daemon.policy = _policy()
    applied = []
    monkeypatch.setattr(Daemon, "_save_and_apply", lambda self: applied.append(True))
    return daemon, applied


def test_setting_additions_records_and_applies(monkeypatch):
    daemon, applied = _daemon(monkeypatch)
    daemon.impl_SetBuildSources(["Mirror.Example.org", "github.com", "mirror.example.org"], "")
    # github.com is built in already; the rest is stored once, as a host
    assert daemon.policy.build_sources_extra == ["mirror.example.org"]
    assert applied


def test_a_bad_entry_changes_nothing(monkeypatch):
    daemon, applied = _daemon(monkeypatch)
    daemon.policy.build_sources_extra = ["mirror.example.org"]
    with pytest.raises(PolicyError):
        daemon.impl_SetBuildSources(["other.example.org", "not a host"], "")
    assert daemon.policy.build_sources_extra == ["mirror.example.org"]
    assert not applied


def test_the_call_is_a_filter_change():
    assert access.ACTIONS["SetBuildSources"] == access.ACTION_MANAGE_FILTER
    assert "SetBuildSources" in access.GUARDIAN_GATED
    assert "SetBuildSources" in access.CHANGES
    assert "SetBuildSources" not in access.PER_USER


# ---- the proxy ----------------------------------------------------------------

@pytest.fixture
def addon(monkeypatch):
    mitmproxy = types.ModuleType("mitmproxy")
    http_mod = types.ModuleType("mitmproxy.http")
    http_mod.HTTPFlow = object
    http_mod.Response = types.SimpleNamespace(
        make=lambda status=200, content=b"", headers=None: types.SimpleNamespace(
            status_code=status, content=content, headers=dict(headers or {})))
    mitmproxy.http = http_mod
    monkeypatch.setitem(sys.modules, "mitmproxy", mitmproxy)
    monkeypatch.setitem(sys.modules, "mitmproxy.http", http_mod)
    spec = importlib.util.spec_from_file_location("kosher_filter_build_sources", ADDON)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    noted = []
    monkeypatch.setattr(module.activity_mod, "record",
                        lambda *a, **k: noted.append((a, k)))
    module.noted = noted
    return module


def _filter(addon, tmp_path, builder_entry):
    rules = tmp_path / "rules.json"
    rules.write_text(json.dumps({
        "1001": {"port": 30001, "rules": [], "blocked_categories": []},
        "builder": {"port": nft.BUILDER_PORT, "rules": [],
                    "blocked_categories": ["adult"], **builder_entry},
    }))
    filt = addon.KosherFilter.__new__(addon.KosherFilter)
    filt.policy = addon.PolicyCache(rules)
    filt.uids = type("U", (), {"uid_for_connection": staticmethod(lambda *a, **k: None)})()
    filt.categories = type("C", (), {
        "blocked_categories_of": staticmethod(
            lambda host, blocked: {"adult"} if host == "adult.github.com" and "adult" in blocked
            else set()),
        "categories_of": staticmethod(lambda host: set()),
    })()
    filt.siterules = type("S", (), {"reason": staticmethod(lambda url: None)})()
    filt.blocklist = type("B", (), {"contains_any": staticmethod(lambda text: False)})()
    return filt


def _flow(url, port):
    from urllib.parse import urlsplit

    parts = urlsplit(url)
    return type("F", (), {
        "request": type("R", (), {
            "pretty_host": parts.hostname, "pretty_url": url,
            "path": parts.path or "/", "method": "GET", "query": {}, "headers": {},
        })(),
        "response": None,
        "client_conn": type("C", (), {
            "peername": ("127.0.0.1", 40000),
            "proxy_mode": type("M", (), {"custom_listen_port": port})(),
        })(),
        "metadata": {},
    })()


def test_a_build_is_refused_a_host_that_is_not_a_source(addon, tmp_path):
    filt = _filter(addon, tmp_path, {"allowed_hosts": list(buildsources.DEFAULT_BUILD_SOURCES)})
    flow = _flow("https://example.com/", nft.BUILDER_PORT)
    filt.request(flow)
    assert flow.response.status_code == 403
    assert flow.response.headers["Content-Type"].startswith("text/plain")
    body = flow.response.content.decode()
    assert "example.com is not a source Nix builds may fetch from" in body
    assert "kosherctl build-sources add example.com" in body
    assert addon.noted and addon.noted[0][1]["why"] == "build-source"


def test_a_build_fetches_from_github(addon, tmp_path):
    filt = _filter(addon, tmp_path, {"allowed_hosts": list(buildsources.DEFAULT_BUILD_SOURCES)})
    flow = _flow("https://raw.githubusercontent.com/NixOS/nixpkgs/master/README.md",
                 nft.BUILDER_PORT)
    filt.request(flow)
    assert flow.response is None


def test_the_categories_still_apply_to_a_source_host(addon, tmp_path):
    filt = _filter(addon, tmp_path, {"allowed_hosts": ["*.github.com"]})
    flow = _flow("https://adult.github.com/x", nft.BUILDER_PORT)
    filt.request(flow)
    assert flow.response.status_code == 403
    assert "adult" in flow.response.content.decode()


def test_an_added_host_is_allowed(addon, tmp_path):
    filt = _filter(addon, tmp_path, {"allowed_hosts": ["github.com", "mirror.example.org"]})
    flow = _flow("https://mirror.example.org/pub/x.tar.gz", nft.BUILDER_PORT)
    filt.request(flow)
    assert flow.response is None


def test_an_older_rules_file_gets_the_built_in_list_not_the_web(addon, tmp_path):
    filt = _filter(addon, tmp_path, {})
    refused = _flow("https://example.com/", nft.BUILDER_PORT)
    filt.request(refused)
    assert refused.response.status_code == 403
    allowed = _flow("https://github.com/NixOS/nixpkgs", nft.BUILDER_PORT)
    filt.request(allowed)
    assert allowed.response is None


def test_an_account_is_not_held_to_the_build_sources(addon, tmp_path):
    filt = _filter(addon, tmp_path, {"allowed_hosts": ["github.com"]})
    flow = _flow("https://example.com/", 30001)
    filt.request(flow)
    assert flow.response is None
