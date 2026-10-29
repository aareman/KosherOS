"""1Password's setgid copies, for 1Password from nixpkgs (#234).

The desktop app trusts `op` only when its group is onepassword-cli, and the
browser helper only as onepassword, so kosherd keeps setgid copies of both
in /run/wrappers/bin. These run as an ordinary user: chown and the store
checks are stood in for.
"""

import json
import os
import stat
from pathlib import Path

import pytest

from kosherd import nixapps, onepassword
from kosherd.onepassword import REFS, WrapperError

REPO = Path(__file__).parents[2]
FILES = REPO / "os-image/files"
GUI = "nixpkgs#_1password-gui"
CLI = "nixpkgs#_1password-cli"


@pytest.fixture
def store(tmp_path, monkeypatch):
    """Fake builds of both packages, and chown that records instead of
    failing. Returns (build, built, chowned)."""
    outs = {"_1password-cli": tmp_path / "store" / "abc-1password-cli-2.33.0",
            "_1password-gui": tmp_path / "store" / "def-1password-8.12.0"}
    (outs["_1password-cli"] / "bin").mkdir(parents=True)
    (outs["_1password-cli"] / "bin" / "op").write_bytes(b"\x7fELF op")
    (outs["_1password-gui"] / "share/1password").mkdir(parents=True)
    (outs["_1password-gui"] / "share/1password/1Password-BrowserSupport").write_bytes(
        b"\x7fELF browser support")
    chowned, built = [], []
    monkeypatch.setattr(onepassword, "source_ok", lambda p: True)
    monkeypatch.setattr(os, "chown", lambda path, uid, gid: chowned.append((Path(path).name, gid)))
    monkeypatch.setattr(onepassword.grp, "getgrnam", lambda name: type(
        "G", (), {"gr_gid": {"onepassword-cli": 851, "onepassword": 852}[name]}))

    def build(attr):
        built.append(attr)
        return outs[attr]

    return build, built, chowned


def _modes(directory):
    return {p.name: stat.S_IMODE(p.stat().st_mode) for p in directory.iterdir()}


def test_anyone_with_either_package_wants_op():
    assert onepassword.wanted({1000: {GUI}})
    assert onepassword.wanted({1000: set(), 1001: {CLI}})
    assert not onepassword.wanted({1000: {"nixpkgs#gh"}, 1001: set()})
    assert REFS == {GUI, CLI}


def test_the_app_brings_op_and_the_browser_helper(tmp_path, store):
    build, built, chowned = store
    wrappers = tmp_path / "wrappers"
    did = onepassword.refresh({1000: {GUI}}, build=build, directory=wrappers,
                              gcroots=tmp_path / "gc")
    assert did == {"op": "installed", "1Password-BrowserSupport": "installed"}
    assert _modes(wrappers) == {"op": 0o2755, "1Password-BrowserSupport": 0o2755}
    assert (wrappers / "op").read_bytes() == b"\x7fELF op"
    assert sorted(gid for _n, gid in chowned) == [851, 852]
    assert dict((n.split(".")[1], gid) for n, gid in chowned) == {
        "op": 851, "1Password-BrowserSupport": 852}
    assert sorted(built) == ["_1password-cli", "_1password-gui"]


def test_the_cli_alone_brings_op_only(tmp_path, store):
    build, built, _ = store
    wrappers = tmp_path / "wrappers"
    did = onepassword.refresh({1000: {CLI}}, build=build, directory=wrappers,
                              gcroots=tmp_path / "gc")
    assert did == {"op": "installed", "1Password-BrowserSupport": "unchanged"}
    assert list(_modes(wrappers)) == ["op"]
    assert built == ["_1password-cli"], "the app is not fetched for op"


def test_nobody_with_1password_means_no_copies_and_no_roots(tmp_path, store):
    build, built, _ = store
    wrappers, gc = tmp_path / "wrappers", tmp_path / "gc"
    onepassword.refresh({1000: {GUI}}, build=build, directory=wrappers, gcroots=gc)
    gc.mkdir()
    (gc / "kosher-_1password-cli").symlink_to("/nix/store/abc")
    (gc / "kosher-_1password-gui").symlink_to("/nix/store/def")
    built.clear()
    did = onepassword.refresh({1000: set()}, build=build, directory=wrappers, gcroots=gc)
    assert did == {"op": "removed", "1Password-BrowserSupport": "removed"}
    assert list(wrappers.iterdir()) == [] and list(gc.iterdir()) == []
    assert built == [], "nothing is built for nobody"


def test_in_step_is_what_the_periodic_check_looks_at(tmp_path, store):
    build, _, _ = store
    wrappers = tmp_path / "wrappers"
    wrappers.mkdir()
    assert onepassword.in_step({}, wrappers)
    assert not onepassword.in_step({1000: {GUI}}, wrappers)
    onepassword.refresh({1000: {GUI}}, build=build, directory=wrappers, gcroots=tmp_path / "g")
    assert onepassword.in_step({1000: {GUI}}, wrappers)
    # Only the CLI now: the browser helper should go.
    assert not onepassword.in_step({1000: {CLI}}, wrappers)


def test_the_copy_comes_from_kosherds_own_rooted_build_not_a_profile(tmp_path):
    # The profile manifest is a file in the account's home; only whether
    # 1Password is there is read from it. The source is kosherd's own
    # build of nixpkgs' attribute, as root, kept by a GC root.
    seen = []

    def run(argv, **_kw):
        seen.append(argv)
        return type("R", (), {"returncode": 0, "stdout": "/nix/store/abc-1password-cli-2.33.0\n",
                              "stderr": ""})

    out = onepassword.build("_1password-cli", run=run, gcroots=tmp_path)
    assert out == Path("/nix/store/abc-1password-cli-2.33.0")
    assert seen[0][-1] == "nixpkgs#_1password-cli"
    assert "--impure" in seen[0] and "NIXPKGS_ALLOW_UNFREE=1" in seen[0]
    assert "--profile" not in seen[0]
    assert seen[0][seen[0].index("--out-link") + 1] == str(tmp_path / "kosher-_1password-cli")


def test_a_failed_build_says_so():
    def run(argv, **_kw):
        return type("R", (), {"returncode": 1, "stdout": "", "stderr": "error: no network\n"})

    with pytest.raises(WrapperError, match="no network"):
        onepassword.build("_1password-cli", run=run)


def test_only_a_root_owned_store_file_is_made_setgid(tmp_path):
    # As an ordinary user nothing here is root's, and nothing is outside
    # the store either: both are refused.
    mine = tmp_path / "op"
    mine.write_bytes(b"x")
    assert not onepassword.source_ok(mine)
    with pytest.raises(WrapperError):
        onepassword.install(mine, "op", "onepassword-cli", tmp_path / "w")


def test_nixpkgs_names_that_start_with_an_underscore_are_refs():
    assert nixapps.package("nixpkgs#_1password-gui") == "_1password-gui"
    for bad in ("nixpkgs#-op", "nixpkgs#_", "nixpkgs#_-x", "nixpkgs#__x"):
        with pytest.raises(nixapps.NixAppError):
            nixapps.package(bad)


def test_the_image_has_the_groups_the_dir_and_the_path():
    sysusers = (FILES / "usr/lib/sysusers.d/kosher-1password.conf").read_text()
    assert "g onepassword-cli 851" in sysusers and "g onepassword 852" in sysusers
    assert "d /run/wrappers/bin 0755 root root" in (
        FILES / "usr/lib/tmpfiles.d/kosher.conf").read_text()
    profile = FILES / "etc/profile.d/zz-kosher-wrappers.sh"
    # After nix.sh, which puts the Nix profile first.
    assert sorted(["nix.sh", "kosher-nix-path.sh", profile.name])[-1] == profile.name
    assert 'PATH="/run/wrappers/bin${PATH:+:$PATH}"' in profile.read_text()
    containerfile = (REPO / "os-image/Containerfile").read_text()
    assert "systemd-sysusers /usr/lib/sysusers.d/kosher-1password.conf" in containerfile


def test_the_store_offers_both():
    import json

    refs = {a["ref"] for a in json.loads(
        (FILES / "etc/kosher/catalog.json").read_text())["apps"]}
    assert REFS <= refs


def test_firefox_finds_the_browser_helper_before_the_app_first_starts():
    # The app writes ~/.mozilla's manifest at every start; until then (and
    # whatever it writes) the system one points at the setgid copy.
    for libdir in ("usr/lib64", "usr/lib"):
        manifest = json.loads((FILES / libdir / "mozilla/native-messaging-hosts"
                               / "com.1password.1password.json").read_text())
        assert manifest["path"] == "/run/wrappers/bin/1Password-BrowserSupport"
        assert manifest["type"] == "stdio"
        assert "{d634138d-c276-4fc8-924b-40a0ea21d284}" in manifest["allowed_extensions"]
