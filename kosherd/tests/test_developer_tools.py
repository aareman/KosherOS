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
from pathlib import Path

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


ROOT = Path(__file__).parents[2]


class _Group:
    def __init__(self, members):
        self.gr_mem = list(members)


def _fake_group(monkeypatch, members):
    import grp

    def getgrnam(name):
        if name == nixdaemon.CLI_GROUP and members is not None:
            return _Group(members)
        raise KeyError(name)
    monkeypatch.setattr(grp, "getgrnam", getgrnam)


def _ran(calls):
    import subprocess

    def run(argv, **kw):
        calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, "", "")
    return run


def test_only_developer_accounts_may_run_nix_at_all(monkeypatch):
    # The daemon's list is not enough: nix builds into a store under ~
    # without the daemon (#214). The nix program is for this group only.
    policy = _policy(
        UserPolicy(uid=1002, username="dev", mode="filtered", developer_tools=True),
        UserPolicy(uid=1001, username="kid", mode="filtered"),
        UserPolicy(uid=1003, username="off", mode="none", developer_tools=True),
    )
    assert nixdaemon.cli_members(policy) == ["dev"]
    calls = []
    _fake_group(monkeypatch, ["kid", "stale"])
    assert nixdaemon.sync_cli_group(policy, run=_ran(calls)) is True
    assert calls == [["gpasswd", "-M", "dev", "kosher-nix"]]


def test_switching_the_last_developer_off_empties_the_group(monkeypatch):
    calls = []
    _fake_group(monkeypatch, ["dev"])
    policy = _policy(UserPolicy(uid=1002, username="dev", mode="filtered"))
    assert nixdaemon.sync_cli_group(policy, run=_ran(calls)) is True
    assert calls == [["gpasswd", "-M", "", "kosher-nix"]]


def test_an_unchanged_group_is_left_alone_and_a_missing_one_ignored(monkeypatch):
    calls = []
    policy = _policy(UserPolicy(uid=1002, username="dev", mode="filtered", developer_tools=True))
    _fake_group(monkeypatch, ["dev"])
    assert nixdaemon.sync_cli_group(policy, run=_ran(calls)) is False
    _fake_group(monkeypatch, None)
    assert nixdaemon.sync_cli_group(policy, run=_ran(calls)) is False
    assert calls == []


def test_the_image_lets_only_root_and_the_group_run_nix():
    containerfile = (ROOT / "os-image/Containerfile").read_text()
    sysusers = (ROOT / "os-image/files/usr/lib/sysusers.d/kosher-nix.conf").read_text()
    # A fixed gid: /usr carries it in the file's owner, and an upgraded
    # machine's /etc/group gets the group from sysusers at boot.
    line = next(l for l in sysusers.splitlines() if l.startswith("g "))
    _, name, gid = line.split()[:3]
    assert name == nixdaemon.CLI_GROUP and gid.isdigit()
    assert f"chown root:{gid} /usr/bin/nix /usr/libexec/nix-nswrapper /usr/lib64/libnix" in containerfile
    assert "chmod 0750 /usr/bin/nix /usr/libexec/nix-nswrapper /usr/lib64/libnix" in containerfile


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
    owned = []
    monkeypatch.setattr(nixapps.shutil, "chown", lambda path, u, g: owned.append((Path(path), u, g)))
    linked = []
    monkeypatch.setattr(nixapps.os, "lchown", lambda path, u, g: linked.append((Path(path), u, g)))
    (tmp_path / "kid").mkdir()
    argv = nixapps._argv(1001, "profile", "install", "nixpkgs#hello", as_root=True)
    assert argv[0] == "env" and "runuser" not in argv
    assert argv[-2:] == ["--profile", str(tmp_path / "kid/.local/state/nix/profiles/profile")]
    assert (tmp_path / "kid/.local/state/nix/profiles").is_dir()
    # Every folder made on the way belongs to the account, never to root:
    # a root ~/.local left a fresh account with no launcher and nowhere
    # for its first sign-in to write.
    made = [tmp_path / "kid" / p for p in
            (".local", ".local/state", ".local/state/nix", ".local/state/nix/profiles")]
    assert owned == [(p, 1001, 1001) for p in made]
    # The session finds Store apps through ~/.nix-profile/bin; nix as root
    # makes no such link, so without it the app was not on the PATH and
    # the desktop hid its launcher.
    link = tmp_path / "kid/.nix-profile"
    assert link.is_symlink()
    assert link.readlink() == tmp_path / "kid/.local/state/nix/profiles/profile"
    assert linked == [(link, 1001, 1001)]
    # ...and a second install changes nothing that is already there.
    owned.clear()
    linked.clear()
    nixapps._argv(1001, "profile", "install", "nixpkgs#hello", as_root=True)
    assert owned == [] and linked == []
    # a developer's own install still runs as the account
    own = nixapps._argv(1001, "profile", "install", "nixpkgs#hello")
    assert own[:3] == ["runuser", "-u", "kid"]


def test_the_store_passes_run_as_root_to_nix_apps_only(monkeypatch):
    # Per-account apps are Nix packages or a developer's own signed build
    # (accountapps). Only Nix goes through the daemon that refuses an
    # account without developer tools, so only Nix is told to run as root;
    # a developer's build is always installed as the account.
    from kosherd import accountapps, nixapps, upstreamapps

    seen = []
    for module, name in ((nixapps, "nix"), (upstreamapps, "upstream")):
        for action in ("install", "upgrade", "remove"):
            monkeypatch.setattr(module, action,
                                lambda ref, uid, progress, *rest, _n=name, **kw:
                                seen.append((_n, rest, kw)))
    for action in ("install", "upgrade", "remove"):
        getattr(accountapps, action)("nixpkgs#hello", 1001, None, as_root=True)
        getattr(accountapps, action)("upstream#kitty", 1001, None, as_root=True)
    assert [(rest, kw) for n, rest, kw in seen if n == "nix"] == [((), {"as_root": True})] * 3
    assert [(rest, kw) for n, rest, kw in seen if n == "upstream"] == [((), {})] * 3
