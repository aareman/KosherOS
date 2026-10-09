"""Apps from nixpkgs in the Store (issue #34): installed into the asking
account's own profile, as that account, under the same approval rule as
a Flatpak.
"""

import json
import os
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
    """A fake Popen: iterable stderr for nixapps._run, and enough of the
    protocol for subprocess.run (the desktop helper call after it)."""

    def __init__(self, lines, code, args=()):
        self.stderr = iter(lines)
        self._code = code
        self.returncode = code
        self.args = list(args)

    def wait(self, timeout=None):
        return self._code

    def poll(self):
        return self._code

    def communicate(self, input=None, timeout=None):
        return "", ""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


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
    nix = [c for c in calls if "nix" in c]  # the desktop helper runs between
    assert nix[0][nix[0].index("nix"):] == ["nix", "profile", "remove", "gh"]
    assert nix[1][nix[1].index("nix"):] == ["nix", "profile", "upgrade", "--impure", "gh"]


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

def test_the_session_environment_is_left_alone():
    # Setting XDG_DATA_DIRS from environment.d replaced the greeter's and
    # took the login screen down ("No GSettings schemas are installed").
    # Desktop entries reach the app grid through ~/.local/share instead.
    for conf in (ROOT / "os-image/files/etc/environment.d").glob("*"):
        assert "XDG_DATA_DIRS" not in conf.read_text(), conf.name
    assert "kosher-nix-desktop" in (ROOT / "kosherd/pyproject.toml").read_text()
    assert "test -x /usr/bin/kosher-nix-desktop" in (ROOT / "os-image/Containerfile").read_text()


def test_desktop_entries_are_linked_after_every_change(home, monkeypatch):
    calls = []

    def popen(argv, **k):
        calls.append(argv)
        return _Proc([], 0)

    def run(argv, **k):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")
    monkeypatch.setattr(nixapps.subprocess, "Popen", popen)
    monkeypatch.setattr(nixapps.subprocess, "run", run)
    nixapps.install("nixpkgs#gh", 1001, lambda *a: None)
    nixapps.remove("nixpkgs#gh", 1001, lambda *a: None)
    nixapps.upgrade("nixpkgs#gh", 1001, lambda *a: None)
    helper = [c for c in calls if c[-2:] == ["kosher-nix-desktop", "-q"]]
    assert len(helper) == 3
    # As the account, with the account's HOME: the links are its own.
    assert helper[0][:3] == ["runuser", "-u", "u1001"]
    assert f"HOME={home / 'u1001'}" in helper[0]


def test_the_desktop_helper_links_and_unlinks(tmp_path, monkeypatch):
    from kosherd import nixdesktop

    monkeypatch.setattr(nixdesktop, "STORE", str(tmp_path / "store"))
    home = tmp_path / "home"
    store = tmp_path / "store" / "abc-cursor"
    (store / "share/applications").mkdir(parents=True)
    (store / "share/icons/hicolor/48x48/apps").mkdir(parents=True)
    (store / "share/applications/cursor.desktop").write_text("[Desktop Entry]\n")
    (store / "share/icons/hicolor/48x48/apps/cursor.png").write_bytes(b"png")
    (store / "share/applications/notes.txt").write_text("not a desktop file")
    profile = home / ".local/state/nix/profiles/profile"
    profile.parent.mkdir(parents=True)
    profile.symlink_to(store)
    (home / ".nix-profile").symlink_to(profile)
    (home / ".local/share/applications").mkdir(parents=True)
    (home / ".local/share/applications/mine.desktop").write_text("theirs")
    stale = home / ".local/share/applications/old.desktop"
    stale.symlink_to(tmp_path / "store" / "gone" / "old.desktop")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)

    assert nixdesktop.sync(home) == (2, 1)
    link = home / ".local/share/applications/cursor.desktop"
    assert link.is_symlink() and os.readlink(link) == str(store / "share/applications/cursor.desktop")
    assert (home / ".local/share/icons/hicolor/48x48/apps/cursor.png").is_symlink()
    assert not (home / ".local/share/applications/notes.txt").exists()
    assert (home / ".local/share/applications/mine.desktop").read_text() == "theirs"
    assert not stale.exists() and not stale.is_symlink()
    # Idempotent.
    assert nixdesktop.sync(home) == (0, 0)
    # The profile moves on without the app: its links go.
    (store / "share/applications/cursor.desktop").unlink()
    assert nixdesktop.sync(home) == (0, 1)


def test_a_terminal_tool_gets_a_launcher_from_the_catalog(tmp_path, monkeypatch):
    # Claude Code has no desktop file: it is a terminal program. The Store
    # installed it, so the app grid shows it, and opening it is a terminal
    # running it — by the catalog's `exec`, through the profile's path.
    from kosherd import nixdesktop

    home = tmp_path / "home"
    profile = home / ".local/state/nix/profiles/profile"
    (profile / "bin").mkdir(parents=True)
    (profile / "share").mkdir()
    (profile / "bin/claude").write_text("#!/bin/sh\n")
    (home / ".nix-profile").symlink_to(profile)
    catalog = tmp_path / "catalog.json"
    catalog.write_text(json.dumps({"apps": [
        {"ref": "nixpkgs#claude-code", "name": "Claude Code", "summary": "Anthropic's coding agent, in the terminal",
         "categories": ["Development", "ConsoleOnly"], "exec": "claude"},
        {"ref": "nixpkgs#codex", "name": "Codex", "summary": "x", "exec": "codex"},   # not installed
        {"ref": "nixpkgs#gh", "name": "GitHub CLI", "summary": "x"},                 # no exec: no launcher
        {"ref": "com.visualstudio.code", "name": "VS Code", "exec": "code"},        # a flatpak: not ours
    ]}))
    monkeypatch.setattr(nixdesktop, "CATALOG", catalog)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)

    assert nixdesktop.sync(home) == (1, 0)
    apps = home / ".local/share/applications"
    assert sorted(p.name for p in apps.iterdir()) == ["kosheros-nixpkgs-claude-code.desktop"]
    text = (apps / "kosheros-nixpkgs-claude-code.desktop").read_text()
    assert "Name=Claude Code\n" in text
    assert f"Exec={home}/.nix-profile/bin/claude\n" in text
    assert "Terminal=true\n" in text
    assert "X-KosherOS-Nix=nixpkgs#claude-code\n" in text
    # Idempotent; and gone when the tool is removed from the profile.
    assert nixdesktop.sync(home) == (0, 0)
    (profile / "bin/claude").unlink()
    assert nixdesktop.sync(home) == (0, 1)
    assert not list(apps.iterdir())


def test_the_shipped_catalog_names_the_command_of_every_terminal_tool():
    apps = json.loads((ROOT / "os-image/files/etc/kosher/catalog.json").read_text())["apps"]
    console = [a for a in apps if "ConsoleOnly" in a.get("categories", [])]
    assert len(console) >= 8
    for app in console:
        assert app["ref"].startswith("nixpkgs#"), app["ref"]
        assert re.fullmatch(r"[a-z][a-z0-9-]*", app.get("exec", "")), app["ref"]
    by_ref = {a["ref"]: a.get("exec") for a in apps}
    # The names nixpkgs gives these (meta.mainProgram), checked by hand.
    assert by_ref["nixpkgs#claude-code"] == "claude"
    assert by_ref["nixpkgs#gemini-cli"] == "gemini"
    assert by_ref["nixpkgs#github-copilot-cli"] == "copilot"
    assert by_ref["nixpkgs#cursor-cli"] == "cursor-agent"
    assert by_ref["nixpkgs#antigravity-cli"] == "agy"


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
