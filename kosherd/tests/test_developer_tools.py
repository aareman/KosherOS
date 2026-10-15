"""Developer tools are a switch, and Nix builds go through the proxy (#123).

Three findings of the October re-audit (#33: N1, N2, N3) had one root: every
account with internet was a Nix daemon user, the build users reached a system
address set full of CDN edges and GitHub, and that set was also what
whitelist-only accounts reached without the proxy. Now developer tools are a
per-account switch, off by default; the registries left the system set; and
the build users' web goes through a proxy listener of their own with their
loopback refused.
"""

import json

import pytest

from kosherd import access, apply, dns, nft, nixdaemon
from kosherd.daemon import Daemon
from kosherd.policy import DEVELOPER_REGISTRIES, Policy, UserPolicy


def _policy(*users: UserPolicy) -> Policy:
    return Policy(revision=1, users=list(users))


def _chain(out: str, name: str) -> list[str]:
    start = out.index(f"chain {name} {{") + len(f"chain {name} {{")
    depth, i = 1, start
    while depth:
        depth += (out[i] == "{") - (out[i] == "}")
        i += 1
    lines = [l.strip() for l in out[start:i - 1].splitlines()]
    return [l for l in lines if l and not l.startswith("#") and not l.startswith("type ")]


# ---- the switch ------------------------------------------------------------------

def test_developer_tools_are_off_by_default_and_round_trip():
    user = UserPolicy(uid=1001, username="kid", mode="filtered")
    assert user.developer_tools is False
    assert "developer_tools" not in user.to_dict()
    dev = UserPolicy(uid=1002, username="dev", mode="filtered", developer_tools=True)
    assert dev.to_dict()["developer_tools"] is True
    doc = _policy(dev).to_dict()
    assert Policy.from_dict(doc).user(1002).developer_tools is True


def test_only_developer_accounts_may_use_the_daemon():
    policy = _policy(
        UserPolicy(uid=1001, username="kid", mode="filtered"),
        UserPolicy(uid=1002, username="dev", mode="filtered", developer_tools=True),
        UserPolicy(uid=1003, username="off", mode="none", developer_tools=True),
    )
    # root, the developer; not the supervised account, not one with no internet
    assert nixdaemon.allowed_users(policy) == ["root", "dev"]
    assert "allowed-users = root dev\n" in nixdaemon.render(policy)


def test_the_switch_is_a_filter_change():
    assert access.ACTIONS["SetDeveloperTools"] == access.ACTION_MANAGE_FILTER
    assert "SetDeveloperTools" in access.GUARDIAN_GATED
    assert "SetDeveloperTools" in access.CHANGES
    assert "SetDeveloperTools" in access.PER_USER


def test_setting_it_records_and_applies(monkeypatch):
    daemon = Daemon.__new__(Daemon)
    daemon.policy = _policy(UserPolicy(uid=1001, username="kid", mode="filtered"))
    applied = []
    monkeypatch.setattr(Daemon, "_save_and_apply", lambda self: applied.append(True))
    daemon.impl_SetDeveloperTools(1001, True, "")
    assert daemon.policy.user(1001).developer_tools is True and applied


# ---- the registries leave the system set ------------------------------------------

def test_the_registries_are_not_in_what_every_account_reaches():
    policy = _policy(UserPolicy(uid=1001, username="kid", mode="whitelist"))
    system = policy.effective_system_whitelist()
    for host in ("github.com", "registry.npmjs.org", "pypi.org", "docker.io"):
        assert host not in system, host


def test_a_developer_on_the_whitelist_gets_the_registries_through_its_own_list():
    kid = UserPolicy(uid=1001, username="kid", mode="whitelist", whitelist=["chinuch.org"])
    dev = UserPolicy(uid=1002, username="dev", mode="whitelist", whitelist=["chinuch.org"],
                     developer_tools=True)
    without = dns.render(_policy(kid))
    with_dev = dns.render(_policy(kid, dev))
    assert "nftset=/github.com/4#inet#kosher#wl4" not in without
    assert "nftset=/github.com/4#inet#kosher#wl4" in with_dev
    assert "nftset=/github.com/4#inet#kosher#sys4" not in with_dev
    for host in DEVELOPER_REGISTRIES[:5]:
        assert host.removeprefix("*.") in with_dev


# ---- the build users -----------------------------------------------------------------

def _render(policy=None, build_gid=999):
    policy = policy or _policy(UserPolicy(uid=1001, username="kid", mode="filtered"))
    return nft.render(policy, dns_uid=988, mitm_uid=987, build_gid=build_gid)


def test_the_builders_web_is_redirected_into_their_own_listener():
    nat = _chain(_render(), "dns_redirect")
    assert f"meta skgid 999 tcp dport {{ 80, 443 }} redirect to :{nft.BUILDER_PORT}" in nat


def test_the_builders_loopback_is_refused_except_their_listener():
    output = _chain(_render(), "output")
    lo_accept = output.index('oif "lo" accept')
    allow = output.index(f"meta skgid 999 ip daddr 127.0.0.0/8 tcp dport {nft.BUILDER_PORT} accept")
    refuse = output.index('meta skgid 999 oif "lo" reject')
    assert allow < refuse < lo_accept
    assert "meta skgid 999 ip daddr 127.0.0.0/8 reject" in output
    assert "meta skgid 999 ip6 daddr ::1 reject" in output


def test_the_builders_get_no_address_set():
    rules = _chain(_render(), "nix_build")
    assert rules == ["jump evasion_block", "reject"]
    assert "@sys4" not in "\n".join(rules)


def test_without_nix_none_of_it_is_rendered():
    out = _render(build_gid=None)
    assert "skgid" not in out and str(nft.BUILDER_PORT) not in out


def test_the_proxy_gets_a_builder_identity(monkeypatch, tmp_path):
    monkeypatch.setattr(apply, "MITM_DIR", tmp_path)
    monkeypatch.setattr(apply, "MITM_RULES_PATH", tmp_path / "rules.json")
    monkeypatch.setattr(nixdaemon, "build_gid", lambda: 999)
    apply.write_mitm_rules(_policy(UserPolicy(uid=1001, username="kid", mode="filtered")))
    doc = json.loads((tmp_path / "rules.json").read_text())
    builder = doc["builder"]
    assert builder["port"] == nft.BUILDER_PORT
    assert "proxy" in builder["blocked_categories"] and "adult" in builder["blocked_categories"]
    assert builder["media_level"] == "none"       # tarballs, not pages
    assert builder["rules"] == []
    modes = (tmp_path / "listen.env").read_text()
    assert f"transparent@127.0.0.1:{nft.BUILDER_PORT}" in modes


def test_without_nix_the_proxy_has_no_builder_identity(monkeypatch, tmp_path):
    monkeypatch.setattr(apply, "MITM_DIR", tmp_path)
    monkeypatch.setattr(apply, "MITM_RULES_PATH", tmp_path / "rules.json")
    monkeypatch.setattr(nixdaemon, "build_gid", lambda: None)
    apply.write_mitm_rules(_policy(UserPolicy(uid=1001, username="kid", mode="filtered")))
    assert "builder" not in json.loads((tmp_path / "rules.json").read_text())


# ---- the Store still installs for everyone --------------------------------------------

def test_a_supervised_accounts_store_install_runs_as_root_into_its_profile(monkeypatch, tmp_path):
    import pwd as pwd_mod
    from kosherd import nixapps

    class PW:
        pw_name, pw_dir, pw_uid, pw_gid = "kid", str(tmp_path / "kid"), 1001, 1001

    monkeypatch.setattr(pwd_mod, "getpwuid", lambda uid: PW())
    argv = nixapps._argv(1001, "profile", "install", "nixpkgs#hello", as_root=True)
    assert argv[0] == "env" and "runuser" not in argv
    assert argv[-2:] == ["--profile", str(tmp_path / "kid/.local/state/nix/profiles/profile")]
    assert (tmp_path / "kid/.local/state/nix/profiles").is_dir()
    # a developer's own install still runs as the account
    own = nixapps._argv(1001, "profile", "install", "nixpkgs#hello")
    assert own[:3] == ["runuser", "-u", "kid"]
