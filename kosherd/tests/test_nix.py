"""Nix on KosherOS (issue #34): the image carries it, the store lives on
/var, the build users are fenced in by the firewall, and who may use the
daemon is the policy's decision.

The daemon runs as root and builds for whoever asks, so it would be an
unfiltered path out of the machine if nothing held it: an account with no
internet could fetch through a build, and a build could fetch any page.
Neither is so — a build user reaches the registries and the system domains
only, and the daemon refuses accounts whose kind of internet is "No
internet".
"""

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from kosherd import apply as apply_mod
from kosherd import nft, nixdaemon
from kosherd.policy import Policy, UserPolicy

ROOT = Path(__file__).parents[2]
CONTAINERFILE = (ROOT / "os-image/Containerfile").read_text()
FILES = ROOT / "os-image/files"
NIX_CONF = (FILES / "etc/nix/nix.conf").read_text()
MOUNT = (FILES / "usr/lib/systemd/system/nix.mount").read_text()
STORE_SERVICE = (FILES / "usr/lib/systemd/system/kosher-nix-store.service").read_text()
LAUNCHER = FILES / "usr/bin/devenv"
BUNDLE = "/etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem"


def _unit(text: str) -> dict:
    return dict(re.findall(r"^([A-Za-z]+)=(.*)$", text, re.M))


# -- the image ---------------------------------------------------------------

def test_the_image_installs_fedoras_nix_with_its_daemon():
    assert re.search(r"dnf -y install nix nix-daemon nix-legacy", CONTAINERFILE)
    # ...and asserts the pieces the rest of this relies on exist.
    assert "getent group nixbld" in CONTAINERFILE
    assert "nix-daemon.socket" in CONTAINERFILE


def test_the_store_mount_and_the_daemon_socket_are_enabled():
    assert re.search(r"systemctl enable nix\.mount nix-daemon\.socket", CONTAINERFILE)
    assert re.search(r"systemctl is-enabled nix\.mount nix-daemon\.socket", CONTAINERFILE)


def test_the_store_is_bound_from_var():
    # The root is read-only; /nix in the image is the packages' skeleton.
    unit = _unit(MOUNT)
    assert unit["What"] == "/var/lib/nix"
    assert unit["Where"] == "/nix"
    assert unit["Type"] == "none"
    assert "bind" in unit["Options"]
    assert unit["Requires"] == "kosher-nix-store.service"
    assert unit["After"] == "kosher-nix-store.service"
    assert unit["WantedBy"] == "local-fs.target"


def test_the_first_boot_copies_the_skeleton_without_clobbering():
    unit = _unit(STORE_SERVICE)
    assert unit["Before"] == "nix.mount"
    assert unit["RequiresMountsFor"] == "/var/lib"
    assert unit["Type"] == "oneshot"
    starts = re.findall(r"^ExecStart=(.*)$", STORE_SERVICE, re.M)
    assert starts[0] == "/usr/bin/mkdir -p /var/lib/nix"
    # -a keeps the nixbld ownership of the store directory; --no-clobber
    # keeps every boot after the first from touching a store in use.
    assert starts[1].startswith("/usr/bin/cp -a --no-clobber")
    assert starts[1].endswith("/nix/. /var/lib/nix/")
    # ...and the copy is relabelled: cp kept the skeleton's default_t on
    # the booted image, and the daemon's socket could not be created.
    assert starts[2] == "/usr/sbin/restorecon -R /var/lib/nix"


def test_the_store_and_the_daemons_socket_carry_labels_init_may_use():
    # Seen on a booted image: nothing names /nix in Fedora's policy, so
    # the socket's label came out default_t and systemd was refused.
    rules = re.findall(r"semanage fcontext -a -t (\w+) '([^']+)'", CONTAINERFILE)
    assert rules == [
        ("var_lib_t", "/nix(/.*)?"),
        ("var_run_t", "/nix/var/nix/daemon-socket(/.*)?"),
        ("var_run_t", "/var/lib/nix/var/nix/daemon-socket(/.*)?"),
    ], "the socket rules must come after the general one: the last match wins"
    assert "restorecon -R /nix" in CONTAINERFILE
    assert "matchpathcon -n /nix/var/nix/daemon-socket/socket" in CONTAINERFILE


def test_devbox_is_pinned_by_hash_and_checked():
    version = re.search(r"^ARG DEVBOX_VERSION=(\S+)$", CONTAINERFILE, re.M).group(1)
    assert re.match(r"\d+\.\d+\.\d+$", version)
    sums = re.findall(r"sum=([0-9a-f]{64})", CONTAINERFILE)
    assert len(sums) == 2 and len(set(sums)) == 2, "one hash per architecture"
    assert "sha256sum -c -" in CONTAINERFILE
    assert 'test "$(/usr/bin/devbox version)" = "${DEVBOX_VERSION}"' in CONTAINERFILE


def test_the_dev_disk_has_room_for_a_developer():
    # 17 GB filled in an afternoon: the OS deployment, Flatpaks, a Nix
    # store. The proxy stopped writing, installs refused, no upgrade could
    # be staged.
    config = (ROOT / "os-image/dev-config.toml").read_text()
    block = config[config.index("[[customizations.filesystem]]"):]
    assert 'mountpoint = "/"' in block
    size = re.search(r'minsize = "(\d+) GiB"', block)
    assert size and int(size.group(1)) >= 40


def test_a_terminal_opens_without_a_failed_units_banner():
    # Seen on the first booted image: "[systemd] Failed Units: 1 mcelog"
    # before every prompt. The CoreOS profile script goes, and mcelog
    # does not start on a virtual machine, where it can only fail.
    assert re.search(r"RUN for p in sudo[^;]*console-login-helper-messages-profile", CONTAINERFILE, re.S)
    assert "! test -e /usr/share/console-login-helper-messages/profile.sh" in CONTAINERFILE
    dropin = (FILES / "usr/lib/systemd/system/mcelog.service.d/10-kosheros-vm.conf").read_text()
    assert "ConditionVirtualization=!vm" in dropin


# -- /etc/nix/nix.conf ---------------------------------------------------------

def _conf() -> dict:
    return dict(re.findall(r"^([a-z-]+) = (.*)$", NIX_CONF, re.M))


def test_the_daemon_is_closed_until_kosherd_opens_it():
    conf = _conf()
    assert conf["allowed-users"] == "root"
    assert conf["trusted-users"] == "root"
    # ...and the file kosherd writes is the last word.
    assert NIX_CONF.rstrip().endswith(f"!include {nixdaemon.CONF_PATH}")


def test_builds_are_sandboxed_as_the_build_users():
    conf = _conf()
    assert conf["sandbox"] == "true"
    assert conf["build-users-group"] == nixdaemon.BUILD_GROUP


def test_nix_trusts_the_filters_certificate():
    assert _conf()["ssl-cert-file"] == BUNDLE
    # The RPM's profile script is patched to name the same bundle first,
    # or it falls back to the shell's own cacert on Fedora 44.
    assert f"NIX_SSL_CERT_FILE={BUNDLE}" in CONTAINERFILE
    assert "grep -q 'NIX_SSL_CERT_FILE=" in CONTAINERFILE


def test_fedoras_own_settings_are_kept():
    conf = _conf()
    assert conf["experimental-features"] == "nix-command flakes"
    assert conf["extra-nix-path"] == "nixpkgs=flake:nixpkgs"
    assert conf["auto-optimise-store"] == "true"


def test_devenvs_cache_is_a_substituter_with_its_published_key():
    conf = _conf()
    assert conf["extra-substituters"] == "https://devenv.cachix.org"
    assert conf["extra-trusted-public-keys"].startswith("devenv.cachix.org-1:")


# -- the launcher -------------------------------------------------------------

def test_the_devenv_launcher_is_executable_and_installs_from_nixpkgs():
    assert os.access(LAUNCHER, os.X_OK)
    text = LAUNCHER.read_text()
    assert text.startswith("#!/bin/sh\n")
    assert "nix profile install nixpkgs#devenv" in text
    # The Containerfile sets the mode too; COPY has lost it before.
    assert "chmod 755 /usr/bin/devenv" in CONTAINERFILE


@pytest.mark.skipif(shutil.which("shellcheck") is None, reason="shellcheck not installed")
def test_the_devenv_launcher_is_valid_sh():
    result = subprocess.run(["shellcheck", "-s", "sh", str(LAUNCHER)],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stdout


# -- the firewall: build users ---------------------------------------------------

def _policy(*modes):
    return Policy(revision=1, users=[
        UserPolicy(uid=1000 + i, username=f"u{i}", mode=m) for i, m in enumerate(modes)])


def _chain(out: str, name: str) -> list[str]:
    body = out.split(f"chain {name} {{")[1].split("\n    }")[0]
    return [l.strip() for l in body.splitlines() if l.strip() and not l.strip().startswith("#")]


def test_build_users_are_dispatched_before_the_system_uid_accept():
    out = nft.render(_policy("filtered"), dns_uid=989, mitm_uid=988, build_gid=999)
    rules = _chain(out, "output")
    assert "meta skgid 999 jump nix_build" in rules
    assert rules.index("meta skgid 999 jump nix_build") < rules.index(f"meta skuid < {nft.UID_MIN} accept")


def test_build_users_reach_the_system_domains_and_nothing_else():
    out = nft.render(_policy("filtered"), dns_uid=989, mitm_uid=988, build_gid=999)
    rules = _chain(out, "nix_build")
    assert rules == [
        "jump evasion_block",
        "ip daddr @sys4 tcp dport { 80, 443 } accept",
        "ip6 daddr @sys6 tcp dport { 80, 443 } accept",
        "reject",
    ]
    # Not the account's own list: a build is nobody's in particular.
    assert "@wl4" not in "\n".join(rules)


def test_without_nix_there_is_no_build_rule():
    out = nft.render(_policy("filtered"), dns_uid=989, mitm_uid=988)
    assert "skgid" not in out


def test_the_ruleset_with_the_build_chain_still_loads(tmp_path):
    if shutil.which("nft") is None:
        pytest.skip("nft not installed")
    path = tmp_path / "k.nft"
    path.write_text(nft.render(_policy("filtered", "whitelist"),
                               dns_uid=989, mitm_uid=988, build_gid=999))
    res = subprocess.run(["nft", "--check", "-f", str(path)], capture_output=True, text=True)
    if res.returncode != 0 and "not permitted" in res.stderr:
        res = subprocess.run(["unshare", "-rn", "nft", "--check", "-f", str(path)],
                             capture_output=True, text=True)
        if "unshare" in res.stderr and res.returncode != 0:
            pytest.skip(res.stderr.strip())
    assert res.returncode == 0, res.stderr


# -- who may use the daemon ----------------------------------------------------

def test_no_internet_means_no_daemon():
    users = nixdaemon.allowed_users(_policy("filtered", "none", "whitelist", "unfiltered"))
    assert users == ["root", "u0", "u2", "u3"]


def test_the_list_is_root_alone_when_nobody_has_internet():
    assert nixdaemon.allowed_users(_policy("none")) == ["root"]
    assert nixdaemon.allowed_users(Policy(revision=1, users=[])) == ["root"]


def test_the_rendered_file_is_one_setting():
    text = nixdaemon.render(_policy("dnsfilter", "none"))
    settings = [l for l in text.splitlines() if l and not l.startswith("#")]
    assert settings == ["allowed-users = root u0"]
    assert "DO NOT EDIT" in text


class _Run:
    def __init__(self):
        self.calls = []

    def __call__(self, argv, **kw):
        self.calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")


def test_apply_writes_the_list_and_restarts_only_on_change(tmp_path, monkeypatch):
    run = _Run()
    monkeypatch.setattr(apply_mod.subprocess, "run", run)
    path = tmp_path / "nix" / "kosheros-users.conf"
    path.parent.mkdir()
    monkeypatch.setattr(apply_mod, "NIX_USERS_PATH", path)

    assert apply_mod.write_nix_users(_policy("filtered")) is True
    assert path.read_text().endswith("allowed-users = root u0\n")
    # try-restart: a daemon nobody has started yet is left for the socket.
    assert run.calls == [["systemctl", "try-restart", "nix-daemon.service"]]

    assert apply_mod.write_nix_users(_policy("filtered")) is False
    assert len(run.calls) == 1, "an unchanged list does not interrupt a build"

    assert apply_mod.write_nix_users(_policy("filtered", "none", "whitelist")) is True
    assert path.read_text().endswith("allowed-users = root u0 u2\n")
    assert len(run.calls) == 2


def test_apply_does_nothing_on_a_machine_without_nix(tmp_path, monkeypatch):
    run = _Run()
    monkeypatch.setattr(apply_mod.subprocess, "run", run)
    monkeypatch.setattr(apply_mod, "NIX_USERS_PATH", tmp_path / "no-nix" / "users.conf")
    assert apply_mod.write_nix_users(_policy("filtered")) is False
    assert not (tmp_path / "no-nix").exists()
    assert run.calls == []


def test_the_build_gid_is_the_nixbld_group_or_nothing(monkeypatch):
    import grp

    class G:
        gr_gid = 999

    monkeypatch.setattr(grp, "getgrnam", lambda name: G() if name == "nixbld" else (_ for _ in ()).throw(KeyError(name)))
    assert nixdaemon.build_gid() == 999
    monkeypatch.setattr(grp, "getgrnam", lambda name: (_ for _ in ()).throw(KeyError(name)))
    assert nixdaemon.build_gid() is None


# -- the docs say so -----------------------------------------------------------

def test_the_supported_page_covers_nix_devenv_and_devbox():
    page = (ROOT / "docs/supported.md").read_text()
    for word in ("nix", "devenv", "devbox", "cache.nixos.org", "No internet"):
        assert word in page, word
