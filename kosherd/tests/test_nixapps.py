"""Apps from nixpkgs in the Store (issue #34): installed into the asking
account's own profile, as that account, under the same approval rule as
a Flatpak.
"""

import json
import re
import subprocess
from pathlib import Path

import pytest

from kosherd import access, apps, nixapps
from kosherd.policy import DEVELOPER_REGISTRIES

ROOT = Path(__file__).parents[2]
BUNDLE = "/etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem"


class _PW:
    def __init__(self, uid, name, home):
        self.pw_uid, self.pw_name, self.pw_dir = uid, name, home


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(nixapps.pwd, "getpwuid", lambda uid: _PW(uid, f"u{uid}", str(tmp_path / f"u{uid}")))
    return tmp_path


def _manifest(home: Path, uid: int, doc: dict) -> None:
    d = home / f"u{uid}" / ".local/state/nix/profiles/profile"
    d.mkdir(parents=True)
    (d / "manifest.json").write_text(json.dumps(doc))


# -- refs ----------------------------------------------------------------------

def test_a_ref_is_the_package_behind_the_prefix():
    assert nixapps.is_nix("nixpkgs#claude-code")
    assert not nixapps.is_nix("com.visualstudio.code")
    assert nixapps.package("nixpkgs#claude-code") == "claude-code"


@pytest.mark.parametrize("bad", ["nixpkgs#", "nixpkgs#../x", "nixpkgs#a b", "nixpkgs#--help",
                                 "nixpkgs#.hidden", "com.example.App"])
def test_a_ref_that_is_not_a_plain_package_name_is_refused(bad):
    # The name is an argument to nix: nothing but an attribute name passes.
    with pytest.raises(nixapps.NixAppError):
        nixapps.package(bad)


# -- what is installed ----------------------------------------------------------

def test_installed_reads_the_profile_manifest(home):
    _manifest(home, 1001, {"version": 3, "elements": {
        "claude-code": {"active": True, "storePaths": ["/nix/store/x-claude-code-2.1"]},
        "gh": {"active": False, "storePaths": ["/nix/store/y-gh-2.1"]},
    }})
    assert nixapps.installed(1001) == {"nixpkgs#claude-code"}


def test_an_older_manifest_lists_elements_by_attribute_path(home):
    _manifest(home, 1001, {"version": 1, "elements": [
        {"attrPath": "legacyPackages.x86_64-linux.neovim", "active": True},
    ]})
    assert nixapps.installed(1001) == {"nixpkgs#neovim"}


def test_no_profile_means_nothing_installed(home):
    assert nixapps.installed(1001) == set()


# -- running nix as the account ---------------------------------------------------

def test_nix_runs_as_the_account_with_its_own_home_and_unfree_allowed(home):
    argv = nixapps._argv(1001, "profile", "install", "--impure", "nixpkgs#claude-code")
    assert argv[:4] == ["runuser", "-u", "u1001", "--"]
    assert argv[4:6] == ["env", "-i"]
    env = {kv.split("=", 1)[0]: kv.split("=", 1)[1] for kv in argv[6:argv.index("nix")]}
    assert env["HOME"] == str(home / "u1001")
    assert env["USER"] == "u1001"
    assert env["NIXPKGS_ALLOW_UNFREE"] == "1"
    assert env["NIX_SSL_CERT_FILE"] == BUNDLE
    assert argv[argv.index("nix"):] == ["nix", "profile", "install", "--impure", "nixpkgs#claude-code"]


class _Proc:
    def __init__(self, lines, code):
        self.stderr = iter(lines)
        self._code = code

    def wait(self):
        return self._code


def test_progress_is_reported_from_nix_log_lines(home, monkeypatch):
    seen = []
    monkeypatch.setattr(nixapps.subprocess, "Popen",
                        lambda *a, **k: _Proc(["evaluating derivation\n", "copying path '/nix/store/x' from 'https://cache.nixos.org'\n",
                                               "building '/nix/store/y.drv'\n"], 0))
    nixapps.install("nixpkgs#gh", 1001, lambda ref, pct, status: seen.append((ref, status)))
    assert seen == [("nixpkgs#gh", "Looking up…"), ("nixpkgs#gh", "Looking up…"),
                    ("nixpkgs#gh", "Downloading…"), ("nixpkgs#gh", "Building…")]


def test_a_failed_install_says_why_in_nixs_words(home, monkeypatch):
    monkeypatch.setattr(nixapps.subprocess, "Popen",
                        lambda *a, **k: _Proc(["copying path\n", "error: cannot connect to socket: user 'u1001' is not allowed to connect to the Nix daemon\n"], 1))
    with pytest.raises(nixapps.NixAppError, match="not allowed to connect"):
        nixapps.install("nixpkgs#gh", 1001, lambda *a: None)


def test_remove_and_upgrade_name_the_package(home, monkeypatch):
    calls = []

    def popen(argv, **k):
        calls.append(argv)
        return _Proc([], 0)
    monkeypatch.setattr(nixapps.subprocess, "Popen", popen)
    nixapps.remove("nixpkgs#gh", 1001, lambda *a: None)
    nixapps.upgrade("nixpkgs#gh", 1001, lambda *a: None)
    assert calls[0][calls[0].index("nix"):] == ["nix", "profile", "remove", "gh"]
    assert calls[1][calls[1].index("nix"):] == ["nix", "profile", "upgrade", "--impure", "gh"]


# -- the queue -------------------------------------------------------------------

def test_the_app_manager_sends_a_nix_ref_to_the_account(monkeypatch):
    done = []
    monkeypatch.setattr(nixapps, "install", lambda ref, uid, progress: done.append((ref, uid)))
    manager = apps.AppManager(lambda *a: None, lambda *a: None)
    manager.install("nixpkgs#gh", permit=lambda r: None, uid=1001)
    manager._worker.join(5)
    assert done == [("nixpkgs#gh", 1001)]


def test_a_nix_app_needs_an_account():
    manager = apps.AppManager(lambda *a: None, lambda *a: None)
    with pytest.raises(apps.AppError, match="for an account"):
        manager.install("nixpkgs#gh", permit=lambda r: None)
    with pytest.raises(apps.AppError, match="for an account"):
        manager.install("nixpkgs#gh", permit=lambda r: None, uid=0)


def test_a_nix_failure_is_reported_like_any_other(monkeypatch):
    finished = []

    def boom(ref, uid, progress):
        raise nixapps.NixAppError("hash mismatch")
    monkeypatch.setattr(nixapps, "install", boom)
    manager = apps.AppManager(lambda *a: None, lambda ref, ok, err: finished.append((ref, ok, err)))
    manager.install("nixpkgs#gh", permit=lambda r: None, uid=1001)
    manager._worker.join(5)
    assert finished == [("nixpkgs#gh", False, "hash mismatch")]


def test_the_approval_rule_is_the_same_as_for_a_flatpak():
    manager = apps.AppManager(lambda *a: None, lambda *a: None)
    with pytest.raises(apps.AppError, match="not on the approved"):
        manager.install("nixpkgs#gh", permit=lambda r: "not on the approved app list", uid=1001)


# -- the daemon knows who is asking -----------------------------------------------

def test_what_is_installed_depends_on_who_asks():
    for method in ("ListInstalled", "UpdateApp", "RemoveApp", "InstallApp"):
        assert method in access.UID_AWARE, method


def test_installed_details_include_the_nix_apps_kosherd_installed(home, monkeypatch):
    _manifest(home, 1001, {"version": 3, "elements": {"gh": {"active": True}}})
    catalog = {"nixpkgs#gh": {"ref": "nixpkgs#gh", "name": "GitHub CLI",
                              "categories": ["Development", "RevisionControl"]}}
    ledger = {"nixpkgs#gh": {"uid": 1001, "username": "u1001"},
              "nixpkgs#codex": {"uid": 1001, "username": "u1001"},   # removed by hand since
              "org.gnome.Maps": {"uid": 1001, "username": "u1001"}}  # a flatpak: not ours
    details = apps.nix_installed_details(catalog, ledger)
    assert [d["ref"] for d in details] == ["nixpkgs#gh"]
    assert details[0]["name"] == "GitHub CLI"
    assert details[0]["installed_by"] == "u1001"
    assert details[0]["kind"] == "develop"
    assert details[0]["approved"] is True


# -- the image ---------------------------------------------------------------------

def test_the_session_sees_the_profiles_desktop_files():
    conf = (ROOT / "os-image/files/etc/environment.d/60-kosher-nix.conf").read_text()
    line = next(l for l in conf.splitlines() if l.startswith("XDG_DATA_DIRS="))
    value = line.split("=", 1)[1]
    assert value.startswith("${XDG_DATA_DIRS:-/usr/local/share:/usr/share}"), "the defaults stay first"
    for path in ("${HOME}/.local/state/nix/profile/share", "${HOME}/.nix-profile/share",
                 "/nix/var/nix/profiles/default/share"):
        assert path in value
    # environment.d knows ${VAR} and ${VAR:-default} and nothing else.
    assert not re.search(r"\$[A-Z_]+[^{A-Z_]", value.replace("${", ""))


def test_the_download_hosts_of_the_unfree_tools_are_registries():
    for host in ("downloads.claude.ai", "downloads.cursor.com", "edgedl.me.gvt1.com",
                 "github.com", "storage.googleapis.com"):
        assert host in DEVELOPER_REGISTRIES, host


def test_check_catalog_asks_nixpkgs_only_where_nix_is(monkeypatch):
    monkeypatch.setattr(nixapps.shutil, "which", lambda name: None)
    assert nixapps.exists("nixpkgs#gh") is None
    monkeypatch.setattr(nixapps.shutil, "which", lambda name: "/usr/bin/nix")
    monkeypatch.setattr(nixapps.subprocess, "run",
                        lambda argv, **k: subprocess.CompletedProcess(argv, 0 if "gh.name" in argv[-1] else 1, "", ""))
    assert nixapps.exists("nixpkgs#gh") is True
    assert nixapps.exists("nixpkgs#no-such-thing") is False
