"""A nixpkgs app the Store installed gets update news like a Flatpak.

The Store's check asked Flathub only, so `kosher update` said "up to date"
while opencode sat two versions behind in the account's profile, and
`kosher upgrade` had nothing to queue although the daemon could upgrade
it all along (UpdateApp). Now the check evaluates the channel for the
caller's own apps, the Updates shelf shows them, and `--local` lets a
developer bring the rest of their own profile along from the terminal.
"""

from __future__ import annotations

import json

import pytest

from kosherd import access, apps, nixapps, storecli
from kosherd.daemon import Daemon

OLD = "/nix/store/42yyi7nyslvn8yqg3phfavmqfan9000y-opencode-2.1.4"
NEW = "/nix/store/lill1ifri3qm4dr928zw1gc3rwh7v8hz-opencode-2.2.5"
GH = "/nix/store/cfsjy2v03pisdix1zkky358m4gb8rmaj-gh-2.102.0"
FONT = "/nix/store/psdcyg2vfq1lngdhw521cqkxzdng6b64-nerd-fonts-fira-code-3.5.0+6.2"

MANIFEST = {"version": 3, "elements": {
    "opencode": {"active": True, "attrPath": "legacyPackages.x86_64-linux.opencode",
                 "originalUrl": "flake:nixpkgs", "storePaths": [OLD]},
    "gh": {"active": True, "attrPath": "legacyPackages.x86_64-linux.gh",
           "originalUrl": "flake:nixpkgs", "storePaths": [GH]},
    "fira-code": {"active": True, "attrPath": "legacyPackages.x86_64-linux.nerd-fonts.fira-code",
                  "originalUrl": "flake:nixpkgs", "storePaths": [FONT]},
    # Pinned to one revision: `nix profile upgrade` cannot move it.
    "hello": {"active": True, "attrPath": "legacyPackages.x86_64-linux.hello",
              "originalUrl": "github:NixOS/nixpkgs/13d0c311", "storePaths": ["/nix/store/x-hello-2.12"]},
}}

CHANNEL = {
    "opencode": {"out": NEW, "version": "2.2.5", "pname": "opencode"},
    "gh": {"out": GH, "version": "2.102.0", "pname": "gh"},
    "nerd-fonts.fira-code": {"out": FONT, "version": "3.5.0+6.2", "pname": "nerd-fonts-fira-code"},
}


# -- reading the profile and the channel ---------------------------------------------

def test_the_profile_gives_the_unlocked_nixpkgs_packages_and_their_paths():
    found = nixapps._profile_paths(MANIFEST)
    assert found == {
        "nixpkgs#opencode": ("opencode", [OLD]),
        "nixpkgs#gh": ("gh", [GH]),
        "nixpkgs#nerd-fonts.fira-code": ("fira-code", [FONT]),
    }, "a pinned package has no latest and is left out"
    assert nixapps._profile_paths({"version": 3}) == {}
    assert nixapps._profile_paths({"elements": "junk"}) == {}


def test_the_channel_is_asked_once_for_every_package():
    expr = nixapps._latest_expr(["opencode", "nerd-fonts.fira-code", "_1password-cli"])
    assert '"opencode" "nerd-fonts.fira-code" "_1password-cli"' in expr
    assert 'builtins.getFlake "nixpkgs"' in expr
    assert "tryEval" in expr, "a package that cannot evaluate is null, not a failure of the lot"


def test_latest_runs_nix_eval_with_the_given_command_and_reads_json(monkeypatch):
    seen = {}

    class R:
        returncode = 0
        stdout = json.dumps({"opencode": CHANNEL["opencode"], "gone": None})
        stderr = ""

    def run(argv, **kw):
        seen["argv"] = argv
        return R()

    monkeypatch.setattr(nixapps.subprocess, "run", run)
    out = nixapps.latest(["opencode", "gone"], ["runuser", "-u", "avi", "--", "nix"])
    assert out["opencode"]["version"] == "2.2.5" and out["gone"] is None
    assert seen["argv"][:5] == ["runuser", "-u", "avi", "--", "nix"]
    assert seen["argv"][5:9] == ["eval", "--impure", "--json", "--expr"]
    assert nixapps.latest([], ["nix"]) == {}, "nothing to ask about, nothing run"


def test_latest_says_what_nix_said_when_it_fails(monkeypatch):
    class R:
        returncode = 1
        stdout = ""
        stderr = "warning: something\nerror: unable to download 'https://channels.nixos.org/...': no network"

    monkeypatch.setattr(nixapps.subprocess, "run", lambda *a, **kw: R())
    with pytest.raises(nixapps.NixAppError, match="unable to download"):
        nixapps.latest(["opencode"], ["nix"])


# -- what has a newer build -------------------------------------------------------------

def test_a_package_whose_channel_build_differs_is_an_update_in_the_flatpak_shape():
    found = nixapps.compare(nixapps._profile_paths(MANIFEST), CHANNEL,
                            names={"nixpkgs#opencode": "OpenCode"})
    assert found == [{
        "ref": "nixpkgs#opencode", "name": "OpenCode",
        "version": "2.1.4", "commit": "42yyi7nyslvn", "latest": "2.2.5",
    }], "gh and the font are at the channel's build; the pinned one is not looked at"


def test_a_package_the_channel_lost_or_cannot_evaluate_is_not_an_update():
    profile = {"nixpkgs#opencode": ("opencode", [OLD])}
    assert nixapps.compare(profile, {"opencode": None}) == []
    assert nixapps.compare(profile, {}) == []


def test_a_rebuild_of_the_same_version_is_an_update_named_by_its_build():
    # The channel rebuilt ripgrep 15.2.0 against a newer libc: the same
    # version, a different path. `nix profile upgrade` would take it, so
    # it is offered, and "15.2.0 -> 15.2.0" would say nothing.
    old = "/nix/store/dzb4833vd9agaaaaaaaaaaaaaaaaaaaa-ripgrep-15.2.0"
    new = "/nix/store/vli6118fdbngwy46j1w741ansmrzfszr-ripgrep-15.2.0"
    found = nixapps.compare({"nixpkgs#ripgrep": ("ripgrep", [old])},
                            {"ripgrep": {"out": new, "version": "15.2.0", "pname": "ripgrep"}})
    assert found == [{"ref": "nixpkgs#ripgrep", "name": "ripgrep", "version": "15.2.0",
                      "commit": "dzb4833vd9ag", "latest": "vli6118fdbng"}]


def test_a_package_with_several_outputs_is_read_from_its_main_one():
    paths = ["/nix/store/knq1mp8bapi9aaaaaaaaaaaaaaaaaaaa-tmux-3.7c-man",
             "/nix/store/knq1mp8bapi9bbbbbbbbbbbbbbbbbbbb-tmux-3.7c"]
    found = nixapps.compare({"nixpkgs#tmux": ("tmux", paths)},
                            {"tmux": {"out": "/nix/store/cccccccccccccccccccccccccccccccc-tmux-3.8",
                                      "version": "3.8", "pname": "tmux"}})
    assert found[0]["version"] == "3.7c" and found[0]["latest"] == "3.8"
    assert found[0]["commit"] == "knq1mp8bapi9", "the main output, not the man pages"


def test_the_installed_version_is_read_off_the_store_path():
    assert nixapps._installed_version(OLD, "opencode") == "2.1.4"
    assert nixapps._installed_version(FONT, "nerd-fonts-fira-code") == "3.5.0+6.2"
    assert nixapps._installed_version("/nix/store/3ja7c0msi88gcx185hpp13c1y8fzang7-1password-cli-2.41.1",
                                      "1password-cli") == "2.41.1"
    # Without a pname to strip, the whole name is better than nothing.
    assert nixapps._installed_version(OLD, "") == "opencode-2.1.4"


def test_updates_looks_only_at_the_stores_refs_in_the_accounts_profile(tmp_path, monkeypatch):
    profile = tmp_path / "profile"
    profile.mkdir()
    (profile / "manifest.json").write_text(json.dumps(MANIFEST))
    monkeypatch.setattr(nixapps, "profile_dir", lambda uid: profile)
    asked = {}

    def latest(attributes, nix):
        asked["attributes"] = attributes
        asked["nix"] = nix
        return CHANNEL

    monkeypatch.setattr(nixapps, "latest", latest)
    monkeypatch.setattr(nixapps, "_argv", lambda uid, *a, as_root=False, profile=True:
                        ["nix-as", str(uid), "root" if as_root else "self", "profile" if profile else "eval"])

    found = nixapps.updates(1000, {"nixpkgs#opencode", "nixpkgs#gh"}, {"nixpkgs#opencode": "OpenCode"})
    assert [u["ref"] for u in found] == ["nixpkgs#opencode"]
    assert sorted(asked["attributes"]) == ["gh", "opencode"], "gh's own package is not the Store's business here"
    assert asked["nix"] == ["nix-as", "1000", "self", "eval"], "evaluation takes no --profile"

    assert nixapps.updates(1000, set()) == [] and "nix" not in asked or asked["nix"][-1] == "eval"
    monkeypatch.setattr(nixapps, "profile_dir", lambda uid: tmp_path / "nowhere")
    assert nixapps.updates(1000, {"nixpkgs#opencode"}) == [], "no profile yet is no update"


def test_nix_eval_as_root_takes_no_profile_argument(monkeypatch):
    pw = type("P", (), {"pw_name": "avi", "pw_dir": "/var/home/avi", "pw_uid": 1000, "pw_gid": 1000})()
    monkeypatch.setattr(nixapps.pwd, "getpwuid", lambda uid: pw)
    monkeypatch.setattr(nixapps, "_make_dirs_as", lambda *a: None)
    monkeypatch.setattr(nixapps, "_link_profile_as", lambda *a: None)
    assert "--profile" not in nixapps._argv(1000, as_root=True, profile=False)
    with_profile = nixapps._argv(1000, "profile", "upgrade", as_root=True)
    assert with_profile[-2:] == ["--profile", "/var/home/avi/.local/state/nix/profiles/profile"]
    as_account = nixapps._argv(1000, profile=False)
    assert as_account[:3] == ["runuser", "-u", "avi"] and "--profile" not in as_account


# -- the daemon -------------------------------------------------------------------------

def _daemon(monkeypatch, developer=True):
    daemon = Daemon.__new__(Daemon)
    monkeypatch.setattr(daemon, "_developer", lambda uid: developer)
    return daemon


def test_the_check_adds_the_callers_nix_apps_and_the_cheap_list_reuses_the_answer(monkeypatch):
    daemon = _daemon(monkeypatch)
    monkeypatch.setattr(apps, "refresh_updates", lambda: [{"ref": "org.x.App", "name": "App"}])
    monkeypatch.setattr(apps, "available_updates", lambda: [{"ref": "org.x.App", "name": "App"}])
    monkeypatch.setattr(apps, "load_catalog", lambda: {"apps": [{"ref": "nixpkgs#opencode", "name": "OpenCode"}]})
    monkeypatch.setattr(apps.accountapps, "installed", lambda uid: {"nixpkgs#opencode", "nixpkgs#gh"})
    asked = []

    def updates(uid, refs, names=None, as_root=False):
        asked.append((uid, sorted(refs), names, as_root))
        return [{"ref": "nixpkgs#opencode", "name": names["nixpkgs#opencode"],
                 "version": "2.1.4", "commit": "42yyi7nyslvn", "latest": "2.2.5"}]

    monkeypatch.setattr(apps.nixapps, "updates", updates)

    found = json.loads(daemon.impl_CheckAppUpdates(_uid=1000).unpack()[0])
    assert [u["ref"] for u in found] == ["org.x.App", "nixpkgs#opencode"]
    assert asked == [(1000, ["nixpkgs#gh", "nixpkgs#opencode"], {"nixpkgs#opencode": "OpenCode"}, False)]

    listed = json.loads(daemon.impl_ListAppUpdates(_uid=1000).unpack()[0])
    assert [u["ref"] for u in listed] == ["org.x.App", "nixpkgs#opencode"]
    assert len(asked) == 1, "the Store opening does not evaluate nixpkgs again"
    # Another account's list is its own: nothing was checked for it.
    assert [u["ref"] for u in json.loads(daemon.impl_ListAppUpdates(_uid=1001).unpack()[0])] == ["org.x.App"]


def test_a_nix_check_that_fails_still_answers_with_the_flatpaks(monkeypatch):
    daemon = _daemon(monkeypatch)
    monkeypatch.setattr(apps, "refresh_updates", lambda: [{"ref": "org.x.App", "name": "App"}])
    monkeypatch.setattr(apps.accountapps, "installed", lambda uid: {"nixpkgs#opencode"})
    monkeypatch.setattr(apps, "load_catalog", lambda: {"apps": []})

    def boom(*a, **kw):
        raise nixapps.NixAppError("no network")

    monkeypatch.setattr(apps.nixapps, "updates", boom)
    assert json.loads(daemon.impl_CheckAppUpdates(_uid=1000).unpack()[0]) == [{"ref": "org.x.App", "name": "App"}]


def test_an_account_without_developer_tools_is_checked_as_root(monkeypatch):
    daemon = _daemon(monkeypatch, developer=False)
    monkeypatch.setattr(apps, "refresh_updates", lambda: [])
    monkeypatch.setattr(apps, "load_catalog", lambda: {"apps": []})
    monkeypatch.setattr(apps.accountapps, "installed", lambda uid: {"nixpkgs#opencode"})
    seen = {}
    monkeypatch.setattr(apps.nixapps, "updates",
                        lambda uid, refs, names=None, as_root=False: seen.update(as_root=as_root) or [])
    daemon.impl_CheckAppUpdates(_uid=1000)
    assert seen == {"as_root": True}
    assert json.loads(daemon.impl_CheckAppUpdates(_uid=0).unpack()[0]) == [], "root has no profile of its own"


def test_update_all_queues_the_callers_nix_apps_as_the_caller_and_forgets_them(monkeypatch):
    daemon = _daemon(monkeypatch)
    queued = []

    class Manager:
        pending = set()

        def update(self, ref, uid=None, as_root=False):
            queued.append((ref, uid, as_root))

    daemon.app_manager = Manager()
    monkeypatch.setattr(apps, "available_updates", lambda: [{"ref": "org.x.App"}])
    daemon._nix_updates_known()[1000] = [{"ref": "nixpkgs#opencode"}, {"ref": "nixpkgs#gh"}]
    daemon._nix_updates_known()[1001] = [{"ref": "nixpkgs#codex"}]

    assert daemon.impl_UpdateAllApps(_uid=1000).unpack()[0] == 3
    assert queued == [("org.x.App", None, False),
                      ("nixpkgs#opencode", 1000, False), ("nixpkgs#gh", 1000, False)]
    assert daemon._nix_updates_known()[1000] == [], "queued, so no longer offered"
    assert daemon._nix_updates_known()[1001] == [{"ref": "nixpkgs#codex"}], "another account's are not the caller's to take"
    assert json.loads(daemon.impl_ListAppUpdates(_uid=1000).unpack()[0]) == [{"ref": "org.x.App"}]


def test_updating_one_nix_app_by_hand_forgets_it_too(monkeypatch):
    daemon = _daemon(monkeypatch)
    queued = []

    class Manager:
        def update(self, ref, uid=None, as_root=False):
            queued.append((ref, uid, as_root))

    daemon.app_manager = Manager()
    monkeypatch.setattr(daemon, "_nix_owner", lambda ref, uid, others=False: uid)
    daemon._nix_updates_known()[1000] = [{"ref": "nixpkgs#opencode"}]
    daemon.impl_UpdateApp("nixpkgs#opencode", _uid=1000)
    assert queued == [("nixpkgs#opencode", 1000, False)]
    assert daemon._nix_updates_known()[1000] == []


def test_the_update_methods_know_who_is_asking():
    for method in ("ListAppUpdates", "CheckAppUpdates", "UpdateAllApps"):
        assert method in access.UID_AWARE, method


# -- kosher --local ----------------------------------------------------------------------

class _Client:
    def __init__(self, updates=(), installed=("nixpkgs#opencode",)):
        self.calls = []
        self.updates = list(updates)
        self.installed = list(installed)

    def check_app_updates(self):
        self.calls.append("check")
        return self.updates

    def list_app_updates(self):
        return self.updates

    def list_installed(self):
        return list(self.installed)

    def update_app(self, ref):
        self.calls.append(("update", ref))


def _wait_ok(client, refs, out=None):
    client.calls.append(("wait", tuple(refs)))
    return True


def _args(**kw):
    return type("A", (), kw)()


OPENCODE = {"ref": "nixpkgs#opencode", "name": "OpenCode", "version": "2.1.4", "commit": "42yyi7nyslvn", "latest": "2.2.5"}
BTOP = {"ref": "nixpkgs#btop", "name": "btop", "version": "1.4.7", "commit": "bvxzrvhzmkmd", "latest": "1.4.8"}


def test_update_without_local_says_nothing_about_the_persons_own_profile(monkeypatch, capsys):
    monkeypatch.setattr(nixapps, "own_updates", lambda: [OPENCODE, BTOP])
    c = _Client(updates=[OPENCODE])
    assert storecli.cmd_update(_args(local=False), c) == 0
    out = capsys.readouterr().out
    assert "nixpkgs#opencode" in out and "2.1.4 -> 2.2.5" in out and "btop" not in out
    assert "1 can be upgraded: kosher upgrade" in out


def test_update_local_adds_the_persons_own_packages_less_the_stores(monkeypatch, capsys):
    monkeypatch.setattr(nixapps, "own_updates", lambda: [OPENCODE, BTOP])
    c = _Client(updates=[OPENCODE])
    assert storecli.cmd_update(_args(local=True), c) == 0
    out = capsys.readouterr().out
    assert out.count("nixpkgs#opencode") == 1, "the Store's answer is not repeated as the person's own"
    assert "your own nix profile" in out and "nixpkgs#btop" in out and "1.4.7 -> 1.4.8" in out
    assert "2 can be upgraded: kosher upgrade --local" in out


def test_update_local_with_nothing_anywhere_says_so(monkeypatch, capsys):
    monkeypatch.setattr(nixapps, "own_updates", lambda: [])
    assert storecli.cmd_update(_args(local=True), _Client()) == 0
    assert "so is your own nix profile" in capsys.readouterr().out


def test_update_local_still_answers_when_nix_cannot_look(monkeypatch, capsys):
    def boom():
        raise nixapps.NixAppError("no network")

    monkeypatch.setattr(nixapps, "own_updates", boom)
    assert storecli.cmd_update(_args(local=True), _Client(updates=[OPENCODE])) == 0
    captured = capsys.readouterr()
    assert "could not check your own nix profile: no network" in captured.err
    assert "nixpkgs#opencode" in captured.out


def test_upgrade_local_takes_the_stores_through_the_daemon_and_its_own_with_nix(monkeypatch, capsys):
    monkeypatch.setattr(nixapps, "own_updates", lambda: [BTOP])
    ran = []
    monkeypatch.setattr(nixapps, "own_upgrade", lambda ref: ran.append(ref) or 0)
    c = _Client(updates=[OPENCODE])
    assert storecli.cmd_upgrade(_args(local=True, apps=[]), c, wait=_wait_ok) == 0
    assert c.calls == [("update", "nixpkgs#opencode"), ("wait", ("nixpkgs#opencode",))]
    assert ran == ["nixpkgs#btop"]
    out = capsys.readouterr().out
    assert "upgrading nixpkgs#btop (your own nix profile)" in out


def test_upgrade_local_by_name_and_a_failing_nix_is_a_failure(monkeypatch, capsys):
    monkeypatch.setattr(nixapps, "own_updates", lambda: [BTOP])
    monkeypatch.setattr(nixapps, "own_upgrade", lambda ref: 1)
    c = _Client(updates=[OPENCODE])
    assert storecli.cmd_upgrade(_args(local=True, apps=["btop"]), c, wait=_wait_ok) == 1
    assert c.calls == [], "nothing of the Store's was named"
    assert "nix could not upgrade it" in capsys.readouterr().err


def test_upgrade_without_local_does_not_know_the_persons_own_packages(monkeypatch, capsys):
    monkeypatch.setattr(nixapps, "own_updates", lambda: [BTOP])
    c = _Client(updates=[])
    assert storecli.cmd_upgrade(_args(local=False, apps=[]), c, wait=_wait_ok) == 0
    assert "--local" in capsys.readouterr().out
    assert storecli.cmd_upgrade(_args(local=False, apps=["btop"]), c, wait=_wait_ok) == 1


def test_the_command_line_has_local_on_update_and_upgrade():
    parser = storecli.build_parser()
    assert parser.parse_args(["update", "--local"]).local is True
    args = parser.parse_args(["upgrade", "--local", "btop"])
    assert args.local is True and args.apps == ["btop"]
    assert parser.parse_args(["upgrade"]).local is False
