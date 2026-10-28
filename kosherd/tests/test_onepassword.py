"""1Password's op, setgid, for 1Password from nixpkgs (#234).

The desktop app trusts `op` only when its group is onepassword-cli, so
kosherd keeps a setgid copy at /run/wrappers/bin/op. These run as an
ordinary user: chown and the store checks are stood in for.
"""

import os
import stat
from pathlib import Path

import pytest

from kosherd import nixapps, onepassword
from kosherd.onepassword import REFS, WrapperError

REPO = Path(__file__).parents[2]
FILES = REPO / "os-image/files"


@pytest.fixture
def store(tmp_path, monkeypatch):
    """A fake built CLI, and chown that records instead of failing."""
    out = tmp_path / "store" / "abc-1password-cli-2.33.0"
    (out / "bin").mkdir(parents=True)
    (out / "bin" / "op").write_bytes(b"\x7fELF op binary")
    chowned = []
    monkeypatch.setattr(onepassword, "source_ok", lambda p: True)
    monkeypatch.setattr(os, "chown", lambda path, uid, gid: chowned.append((uid, gid)))
    monkeypatch.setattr(onepassword.grp, "getgrnam",
                        lambda name: type("G", (), {"gr_gid": {"onepassword-cli": 851}[name]}))
    return out, chowned


def test_anyone_with_either_package_wants_op():
    assert onepassword.wanted({1000: {"nixpkgs#_1password-gui"}})
    assert onepassword.wanted({1000: set(), 1001: {"nixpkgs#_1password-cli"}})
    assert not onepassword.wanted({1000: {"nixpkgs#gh"}, 1001: set()})
    assert REFS == {"nixpkgs#_1password-gui", "nixpkgs#_1password-cli"}


def test_op_is_a_setgid_copy_of_the_built_cli(tmp_path, store):
    out, chowned = store
    wrappers = tmp_path / "wrappers"
    did = onepassword.refresh({1000: {"nixpkgs#_1password-gui"}},
                              build=lambda: out, directory=wrappers)
    assert did == "installed"
    op = wrappers / "op"
    assert op.read_bytes() == b"\x7fELF op binary"
    assert stat.S_IMODE(op.stat().st_mode) == 0o2755
    assert chowned == [(0, 851)]
    assert not [p for p in wrappers.iterdir() if p.name.startswith(".")], "no temp left"


def test_nobody_with_1password_means_no_op(tmp_path, store):
    out, _ = store
    wrappers = tmp_path / "wrappers"
    onepassword.refresh({1000: {"nixpkgs#_1password-cli"}}, build=lambda: out,
                        directory=wrappers)
    built = []
    did = onepassword.refresh({1000: set()}, build=lambda: built.append(1) or out,
                              directory=wrappers)
    assert did == "removed" and not (wrappers / "op").exists()
    assert built == [], "nothing is built for nobody"
    assert onepassword.refresh({}, directory=wrappers) == "unchanged"


def test_the_copy_comes_from_kosherds_own_build_not_a_profile():
    # The profile manifest is a file in the account's home; only whether
    # 1Password is there is read from it. The source is what build_cli
    # returns, and build_cli builds nixpkgs' attribute as root.
    seen = []

    def run(argv, **_kw):
        seen.append(argv)
        return type("R", (), {"returncode": 0, "stdout": "/nix/store/abc-1password-cli-2.33.0\n",
                              "stderr": ""})

    assert onepassword.build_cli(run=run) == Path("/nix/store/abc-1password-cli-2.33.0")
    assert seen[0][-1] == "nixpkgs#_1password-cli"
    assert "--impure" in seen[0] and "NIXPKGS_ALLOW_UNFREE=1" in seen[0]
    assert "--profile" not in seen[0]


def test_a_failed_build_says_so():
    def run(argv, **_kw):
        return type("R", (), {"returncode": 1, "stdout": "", "stderr": "error: no network\n"})

    with pytest.raises(WrapperError, match="no network"):
        onepassword.build_cli(run=run)


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
