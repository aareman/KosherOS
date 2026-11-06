"""What a Nix build may download (#199).

Once the build users could resolve names (#190), a fixed-output build
fetched through the builders' proxy listener as a supervised identity
judged by the default categories alone, so a developer account's own,
stricter filter did not reach its builds. Now the builders' entry in the
proxy's rules file carries each developer account's filter and the proxy
holds a build to all of them: the strictest wins, an account on approved
sites only holds builds to the known source hosts, and with no developer
account a build fetches nothing. A refusal is plain text the build log
shows.
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
    # The Store's 1Password, whose builds fetch the publisher's release (#234).
    "cache.agilebits.com", "downloads.1password.com",
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


def _rules_file(monkeypatch, tmp_path, policy) -> dict:
    monkeypatch.setattr(apply, "MITM_DIR", tmp_path)
    monkeypatch.setattr(apply, "MITM_RULES_PATH", tmp_path / "rules.json")
    monkeypatch.setattr(nixdaemon, "build_gid", lambda: 999)
    apply.write_mitm_rules(policy)
    return json.loads((tmp_path / "rules.json").read_text())["builder"]


def test_the_proxy_rules_carry_each_developer_filter(monkeypatch, tmp_path):
    policy = Policy(revision=1, build_sources_extra=["mirror.example.org"], users=[
        UserPolicy(uid=1001, username="dev", mode="filtered", developer_tools=True,
                   rules=[{"action": "block", "pattern": "example.org"}],
                   blocked_categories=["adult"]),
        UserPolicy(uid=1002, username="wl", mode="whitelist", developer_tools=True,
                   whitelist=["torah.example.net"]),
        UserPolicy(uid=1003, username="kid", mode="filtered"),
        UserPolicy(uid=1004, username="off", mode="none", developer_tools=True),
    ])
    builder = _rules_file(monkeypatch, tmp_path, policy)
    assert builder["port"] == nft.BUILDER_PORT
    assert builder["rules"] == [] and builder["blocked_categories"] == []
    devs = {d["username"]: d for d in builder["developers"]}
    assert set(devs) == {"dev", "wl"}
    assert devs["dev"]["rules"] == [{"action": "block", "pattern": "example.org"}]
    assert devs["dev"]["blocked_categories"] == ["adult"]
    assert "allowed_hosts" not in devs["dev"]
    hosts = devs["wl"]["allowed_hosts"]
    assert "github.com" in hosts and "mirror.example.org" in hosts
    assert "*.torah.example.net" in hosts
    assert "example.com" not in hosts


def test_no_developer_account_writes_an_empty_list(monkeypatch, tmp_path):
    assert _rules_file(monkeypatch, tmp_path, _policy())["developers"] == []


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
                    "blocked_categories": [], **builder_entry},
    }))
    filt = addon.KosherFilter.__new__(addon.KosherFilter)
    filt.policy = addon.PolicyCache(rules)
    filt.uids = type("U", (), {"uid_for_connection": staticmethod(lambda *a, **k: None)})()
    filt.categories = type("C", (), {
        "blocked_categories_of": staticmethod(
            lambda host, blocked: {"adult"} if host.startswith("adult.") and "adult" in blocked
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


def _build(addon, tmp_path, developers, url):
    """A build's download of `url`, with these developer accounts (as
    developer_filters writes them). Returns the response, or None."""
    filt = _filter(addon, tmp_path, {"developers": developers})
    flow = _flow(url, nft.BUILDER_PORT)
    filt.request(flow)
    return flow.response


def _refused(response, *words):
    assert response is not None and response.status_code == 403
    assert response.headers["Content-Type"].startswith("text/plain")
    body = response.content.decode()
    for word in words:
        assert word in body, body
    return body


def _dev(name, mode, rules=(), categories=()):
    return {"username": name, "mode": mode, "rules": list(rules),
            "blocked_categories": list(categories)}


DEFAULT_CATEGORIES = ["adult"]
BLOCK_EXAMPLE_ORG = [{"action": "block", "pattern": "example.org"}]


def _whitelist_dev(name="wl", extra=(), whitelist=()):
    policy = Policy(revision=1, build_sources_extra=list(extra), users=[
        UserPolicy(uid=1002, username=name, mode="whitelist", developer_tools=True,
                   whitelist=list(whitelist), blocked_categories=DEFAULT_CATEGORIES)])
    return buildsources.developer_filters(policy)[0]


# One filtered developer: its own filter, nothing more and nothing less.

def test_a_filtered_developer_rule_reaches_its_builds(addon, tmp_path):
    dev = _dev("avi", "filtered", BLOCK_EXAMPLE_ORG, DEFAULT_CATEGORIES)
    body = _refused(_build(addon, tmp_path, [dev], "https://example.org/x.tar.gz"),
                    "example.org", "avi's account", "rule")
    assert "build-sources" not in body
    assert addon.noted[-1][1]["why"] == "rule:example.org"
    assert _build(addon, tmp_path, [dev], "https://example.com/x.tar.gz") is None


def test_a_filtered_developer_categories_reach_its_builds(addon, tmp_path):
    dev = _dev("avi", "filtered", categories=DEFAULT_CATEGORIES)
    _refused(_build(addon, tmp_path, [dev], "https://adult.example.com/"),
             "adult.example.com", "avi's account", "adult")
    assert addon.noted[-1][1]["why"] == "category:adult"


def test_an_allow_rule_beats_that_account_categories(addon, tmp_path):
    dev = _dev("avi", "filtered", [{"action": "allow", "pattern": "adult.example.com"}],
               DEFAULT_CATEGORIES)
    assert _build(addon, tmp_path, [dev], "https://adult.example.com/") is None


def test_a_dnsfilter_developer(addon, tmp_path):
    dev = _dev("dina", "dnsfilter", BLOCK_EXAMPLE_ORG, DEFAULT_CATEGORIES)
    _refused(_build(addon, tmp_path, [dev], "https://example.org/"), "dina's account")
    _refused(_build(addon, tmp_path, [dev], "https://adult.example.com/"), "dina's account")
    assert _build(addon, tmp_path, [dev], "https://example.com/") is None


# A developer on approved sites only: the source hosts and its own list.

def test_a_whitelist_developer_holds_builds_to_the_sources(addon, tmp_path):
    dev = _whitelist_dev()
    body = _refused(_build(addon, tmp_path, [dev], "https://example.com/"),
                    "example.com is not a source Nix builds may fetch from",
                    "wl's account is on approved sites only",
                    "kosherctl build-sources add example.com")
    assert "wl's approved sites" in body
    assert addon.noted[-1][1]["why"] == "build-source"
    assert _build(addon, tmp_path, [dev],
                  "https://raw.githubusercontent.com/NixOS/nixpkgs/master/README.md") is None


def test_a_host_on_its_own_whitelist_is_allowed(addon, tmp_path):
    dev = _whitelist_dev(whitelist=["torah.example.net"])
    assert _build(addon, tmp_path, [dev], "https://torah.example.net/a.tar.gz") is None
    assert _build(addon, tmp_path, [dev], "https://dl.torah.example.net/a.tar.gz") is None


def test_an_admin_added_host_is_allowed(addon, tmp_path):
    dev = _whitelist_dev(extra=["mirror.example.org"])
    assert _build(addon, tmp_path, [dev], "https://mirror.example.org/pub/x.tar.gz") is None


def test_the_categories_still_apply_to_a_source_host(addon, tmp_path):
    dev = _whitelist_dev(extra=["*.example.org"])
    _refused(_build(addon, tmp_path, [dev], "https://adult.example.org/x"), "adult")


# Two developers: a build must pass both.

def test_two_developers_the_strictest_wins(addon, tmp_path):
    open_dev = _dev("avi", "filtered", categories=DEFAULT_CATEGORIES)
    strict = _dev("dina", "filtered", BLOCK_EXAMPLE_ORG, DEFAULT_CATEGORIES)
    devs = [open_dev, strict]
    _refused(_build(addon, tmp_path, devs, "https://example.org/"), "dina's account")
    assert _build(addon, tmp_path, devs, "https://example.com/") is None
    devs = [open_dev, _whitelist_dev()]
    _refused(_build(addon, tmp_path, devs, "https://example.com/"), "wl's account")
    assert _build(addon, tmp_path, devs, "https://github.com/NixOS/nixpkgs") is None


# No developers: nothing.

def test_no_developer_account_fetches_nothing(addon, tmp_path):
    for url in ("https://example.com/", "https://github.com/NixOS/nixpkgs"):
        _refused(_build(addon, tmp_path, [], url), "no account", "developer tools")


def test_an_older_rules_file_gets_the_built_in_list_not_the_web(addon, tmp_path):
    filt = _filter(addon, tmp_path, {})
    refused = _flow("https://example.com/", nft.BUILDER_PORT)
    filt.request(refused)
    _refused(refused.response, "example.com is not a source")
    allowed = _flow("https://github.com/NixOS/nixpkgs", nft.BUILDER_PORT)
    filt.request(allowed)
    assert allowed.response is None


def test_an_account_is_not_held_to_the_build_sources(addon, tmp_path):
    filt = _filter(addon, tmp_path, {"developers": []})
    flow = _flow("https://example.com/", 30001)
    filt.request(flow)
    assert flow.response is None
