"""Rootless containers under the account's own filter (issue #34).

Podman only, rootless, with `docker` as its shim. What this guards: each
account has a block of subordinate ids and the firewall filters the block
as the account; every container trusts the filter's certificate; the
registries are reachable; and no root daemon ships.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from kosherd import apply as apply_mod
from kosherd import containers, nft
from kosherd.policy import DEVELOPER_REGISTRIES, Policy, UserPolicy

ROOT = Path(__file__).parents[2]
CONTAINERFILE = (ROOT / "os-image/Containerfile").read_text()
FILES = ROOT / "os-image/files"
MOUNTS = (FILES / "etc/containers/mounts.conf").read_text()
CONF = (FILES / "etc/containers/containers.conf.d/50-kosheros.conf").read_text()
BUNDLE = "/etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem"


def _policy(*modes, **extra):
    return Policy(revision=1, users=[
        UserPolicy(uid=1000 + i, username=f"u{i}", mode=m, **extra) for i, m in enumerate(modes)])


# -- /etc/subuid ------------------------------------------------------------------

def test_subuid_is_parsed_by_name_or_uid():
    ranges = containers.parse(
        "# comment\nalice:524288:65536\n1001:589824:65536\nbad line\nbob:1:0\n")
    assert ranges == {"alice": [(524288, 589823)], "1001": [(589824, 655359)]}


def test_each_accounts_blocks_are_found_by_name_or_uid():
    users = [UserPolicy(uid=1000, username="alice", mode="filtered"),
             UserPolicy(uid=1001, username="bob", mode="none"),
             UserPolicy(uid=1002, username="carol", mode="whitelist")]
    ranges = {"alice": [(524288, 589823)], "1001": [(589824, 655359)]}
    assert containers.of_users(users, ranges) == {1000: [(524288, 589823)], 1001: [(589824, 655359)]}


def test_the_next_block_does_not_overlap_an_existing_one():
    assert containers.next_free({}) == containers.FIRST
    assert containers.next_free({"a": [(524288, 589823)]}) == 589824
    # A gap below the first block is used before going past the last.
    assert containers.next_free({"a": [(589824, 655359)]}) == 524288
    assert containers.next_free({"a": [(524288, 524288 + 100)]}) == 524288 + 101


def test_ensure_gives_a_block_to_the_accounts_that_lack_one(tmp_path, monkeypatch):
    subuid = tmp_path / "subuid"
    subuid.write_text("u0:524288:65536\n")
    monkeypatch.setattr(containers, "SUBUID", subuid)
    calls = []

    def run(argv, **kw):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")
    users = _policy("filtered", "none", "whitelist").effective_users()
    blocks = containers.ensure(users, run=run)
    assert calls == [
        ["usermod", "--add-subuids", "589824-655359", "--add-subgids", "589824-655359", "u1"],
        ["usermod", "--add-subuids", "655360-720895", "--add-subgids", "655360-720895", "u2"],
    ]
    assert blocks == {1000: [(524288, 589823)], 1001: [(589824, 655359)], 1002: [(655360, 720895)]}


def test_a_failed_usermod_is_logged_and_the_rest_go_on(tmp_path, monkeypatch):
    monkeypatch.setattr(containers, "SUBUID", tmp_path / "missing")

    def run(argv, **kw):
        return subprocess.CompletedProcess(argv, 1 if argv[-1] == "u0" else 0, "", "boom")
    blocks = containers.ensure(_policy("filtered", "dnsfilter").effective_users(), run=run)
    assert 1000 not in blocks
    assert blocks[1001] == [(524288, 589823)]


def test_system_accounts_get_no_block(tmp_path, monkeypatch):
    monkeypatch.setattr(containers, "SUBUID", tmp_path / "missing")
    calls = []
    users = [UserPolicy(uid=999, username="svc", mode="none")]
    assert containers.ensure(users, run=lambda argv, **kw: calls.append(argv)) == {}
    assert calls == []


# -- the firewall maps a block to its owner ------------------------------------------

SUB = {1000: [(524288, 589823)]}


def _render(policy, **kw):
    return nft.render(policy, dns_uid=989, mitm_uid=988, subids=SUB, **kw)


def test_the_block_is_dispatched_to_the_owners_chain():
    out = _render(_policy("filtered", "whitelist"))
    assert "meta skuid vmap { 1000 : jump mode_filtered, 524288-589823 : jump mode_filtered, 1001 : jump mode_whitelist }" in out


def test_the_block_is_redirected_to_the_owners_proxy():
    out = _render(_policy("filtered"))
    assert "meta skuid { 1000, 524288-589823 } tcp dport != { 53, 465, 587, 993 } redirect to :30000" in out


def test_the_block_shares_the_owners_exceptions():
    out = _render(_policy("filtered", extra_ports=[2222], video_calls=False))
    assert "meta skuid { 1000, 524288-589823 } tcp dport { 2222 } accept" in out
    assert "meta skuid { 1000, 524288-589823 } udp dport >= 1024 reject" in out


def test_the_block_shares_the_owners_resolver():
    out = _render(_policy("unfiltered"))
    assert f"meta skuid {{ 1000, 524288-589823 }} udp dport 53 redirect to :{nft.OPEN_DNS_PORT}" in out


def test_without_blocks_nothing_changes():
    out = nft.render(_policy("filtered"), dns_uid=989, mitm_uid=988)
    assert "meta skuid 1000 tcp dport != { 53, 465, 587, 993 } redirect to :30000" in out
    assert "524288" not in out


def test_the_ruleset_with_blocks_still_loads(tmp_path):
    if shutil.which("nft") is None:
        pytest.skip("nft not installed")
    path = tmp_path / "k.nft"
    path.write_text(_render(_policy("filtered", "whitelist", "unfiltered", "dnsfilter"),
                            build_gid=999))
    res = subprocess.run(["nft", "--check", "-f", str(path)], capture_output=True, text=True)
    if res.returncode != 0 and "not permitted" in res.stderr:
        res = subprocess.run(["unshare", "-rn", "nft", "--check", "-f", str(path)],
                             capture_output=True, text=True)
        if "unshare" in res.stderr and res.returncode != 0:
            pytest.skip(res.stderr.strip())
    assert res.returncode == 0, res.stderr


def test_apply_gives_blocks_and_renders_them(tmp_path, monkeypatch):
    seen = {}
    monkeypatch.setattr(apply_mod.containers, "ensure", lambda users: SUB)
    monkeypatch.setattr(apply_mod.nft, "render", lambda policy, **kw: seen.update(kw) or "")
    monkeypatch.setattr(apply_mod.subprocess, "run",
                        lambda argv, **kw: subprocess.CompletedProcess(argv, 0, "", ""))
    monkeypatch.setattr(apply_mod, "dnsmasq_uid", lambda: 989)
    monkeypatch.setattr(apply_mod, "mitm_uid", lambda: 988)
    monkeypatch.setattr(apply_mod, "search_uid", lambda: 987)
    monkeypatch.setattr(apply_mod, "NFT_RULESET_PATH", tmp_path / "k.nft")
    with pytest.raises(Exception):
        # The rest of apply touches the system; the render call is what
        # this checks, and it has happened by the time anything fails.
        apply_mod.apply_policy(_policy("filtered"))
    assert seen["subids"] == SUB


# -- the image ----------------------------------------------------------------------

def test_podman_is_the_engine_and_docker_is_its_shim():
    assert re.search(r"^\s+podman podman-docker podman-compose passt", CONTAINERFILE, re.M)
    assert "test -x /usr/bin/docker" in CONTAINERFILE
    assert "docker-ce" not in CONTAINERFILE and "moby-engine" not in CONTAINERFILE


def test_toolbox_stays_out():
    assert "--exclude=toolbox" in CONTAINERFILE
    assert re.search(r"RUN for p in sudo toolbox", CONTAINERFILE)


def test_every_new_account_gets_a_block():
    assert "SUB_UID_COUNT 65536" in CONTAINERFILE
    assert "SUB_GID_COUNT 65536" in CONTAINERFILE
    assert "SUB_UID_COUNT 0" not in CONTAINERFILE
    assert "grep -q '^SUB_UID_COUNT 65536'" in CONTAINERFILE


def test_every_container_trusts_the_filters_certificate():
    mounts = [l for l in MOUNTS.splitlines() if l and not l.startswith("#")]
    assert mounts == [
        f"{BUNDLE}:/etc/ssl/certs/ca-certificates.crt",
        f"{BUNDLE}:{BUNDLE}",
        f"{BUNDLE}:/etc/ssl/cert.pem",
    ]


def test_the_tools_inside_a_container_are_pointed_at_it():
    env = dict(re.findall(r'^\s+"([A-Z_]+)=([^"]+)",$', CONF, re.M))
    inside = "/etc/ssl/certs/ca-certificates.crt"
    for var in ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "PIP_CERT", "NODE_EXTRA_CA_CERTS",
                "CARGO_HTTP_CAINFO", "GIT_SSL_CAINFO", "NIX_SSL_CERT_FILE"):
        assert env[var] == inside, var
    assert env["DENO_TLS_CA_STORE"] == "system" and env["UV_NATIVE_TLS"] == "1"
    assert CONF.lstrip("#").strip().startswith("KosherOS") and "[containers]" in CONF
    # The same set the host gives a login shell, so a tool behaves the
    # same inside and out.
    host = (FILES / "etc/profile.d/kosher-ca.sh").read_text()
    for var in env:
        if var != "PATH":
            assert var in host, var


def test_the_image_registries_are_reachable_from_every_account():
    for host in ("registry-1.docker.io", "auth.docker.io", "quay.io", "mcr.microsoft.com",
                 "*.data.mcr.microsoft.com", "ghcr.io", "*.fedoraproject.org"):
        assert host in DEVELOPER_REGISTRIES or host in ("ghcr.io", "*.fedoraproject.org"), host
    assert "hub.docker.com" not in DEVELOPER_REGISTRIES, "a site, not a registry"
    policy = Policy(users=[UserPolicy(uid=1001, username="kid", mode="whitelist")])
    for host in ("registry-1.docker.io", "mcr.microsoft.com", "ghcr.io"):
        assert host in policy.effective_system_whitelist()


def test_the_store_carries_the_container_tools():
    apps = json.loads((FILES / "etc/kosher/catalog.json").read_text())["apps"]
    refs = {a["ref"] for a in apps}
    for ref in ("io.podman_desktop.PodmanDesktop", "nixpkgs#devcontainer", "nixpkgs#vscode"):
        assert ref in refs, ref


def test_the_docs_say_how_dev_containers_work():
    page = (ROOT / "docs/supported.md").read_text()
    for words in ("Dev Containers", "dev.containers.dockerPath", "--userns=keep-id",
                  "no Docker daemon", "podman-compose"):
        assert words in page, words
