"""Every commit carries its own pre-release version.

The user builds ISOs and VMs often and needs to know which build a bug came
from, so `VERSION` ticks on every commit through a git hook. These pin the
arithmetic, the file, and the hook's behaviour in a real scratch repo.
"""

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
SCRIPT = ROOT / "scripts/version.py"

spec = importlib.util.spec_from_file_location("version_tool", SCRIPT)
vt = importlib.util.module_from_spec(spec)
sys.modules["version_tool"] = vt
spec.loader.exec_module(vt)


@pytest.mark.parametrize("current,expected", [
    ("0.1.0-pre.1", "0.1.0-pre.002"),
    ("0.1.0-pre.009", "0.1.0-pre.010"),
    ("0.1.0-pre.99", "0.1.0-pre.100"),
    ("0.1.0-pre.999", "0.1.0-pre.1000"),   # past three digits it simply grows
    ("0.1.0", "0.1.1-pre.001"),   # a release: the next patch starts developing
    ("1.0", "1.0.1-pre.001"),      # the old two-part form is read as X.Y.0
    ("2.3.7\n", "2.3.8-pre.001"),
])
def test_the_next_version(current, expected):
    assert vt.next_version(current) == expected


@pytest.mark.parametrize("bad", ["", "0.1.0-rc.1", "v0.1.0", "0.1.0-pre", "banana"])
def test_anything_else_is_refused(bad):
    with pytest.raises(ValueError):
        vt.next_version(bad)


def test_the_repo_version_is_a_prerelease_the_hook_can_tick():
    text = (ROOT / "VERSION").read_text()
    major, minor, patch, pre = vt.parse(text)
    assert (major, minor) == (0, 1)
    assert pre is not None, "between releases VERSION carries a -pre.N counter"


def _repo(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "t@example"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "t"], check=True)
    (tmp_path / "VERSION").write_text("0.1.0-pre.4\n")
    subprocess.run(["git", "-C", str(tmp_path), "add", "VERSION"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-q", "-m", "start"], check=True)
    return tmp_path


def test_bump_writes_and_stages_the_new_version(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    monkeypatch.delenv("KOSHER_NO_BUMP", raising=False)
    assert vt.bump(repo / "VERSION") == "0.1.0-pre.005"
    assert (repo / "VERSION").read_text() == "0.1.0-pre.005\n"
    staged = subprocess.run(["git", "-C", str(repo), "diff", "--cached", "--name-only"],
                            capture_output=True, text=True, check=True).stdout.split()
    assert staged == ["VERSION"], "the bump must land in the commit being made"


def test_a_merge_commit_is_not_bumped(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    monkeypatch.delenv("KOSHER_NO_BUMP", raising=False)
    (repo / ".git/MERGE_HEAD").write_text("0" * 40 + "\n")
    assert vt.bump(repo / "VERSION") is None
    assert (repo / "VERSION").read_text() == "0.1.0-pre.4\n"


def test_the_bump_can_be_switched_off_for_one_commit(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    monkeypatch.setenv("KOSHER_NO_BUMP", "1")
    assert vt.bump(repo / "VERSION") is None


def test_the_hook_is_installed_by_the_dev_shell():
    nix = (ROOT / "devenv.nix").read_text()
    assert "git-hooks" in nix
    assert "scripts/version.py bump" in nix
    assert "always_run = true" in nix and "pass_filenames = false" in nix


def test_the_command_line(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(vt, "VERSION_FILE", tmp_path / "VERSION")
    (tmp_path / "VERSION").write_text("0.1.0-pre.7\n")
    assert vt.main(["show"]) == 0
    assert vt.main(["next"]) == 0
    out = capsys.readouterr().out.split()
    assert out == ["0.1.0-pre.7", "0.1.0-pre.8"]
    assert vt.main(["nonsense"]) == 2


def test_versions_sort_the_same_way_as_text_and_as_numbers():
    # GitHub orders releases by comparing the tag as text, which put
    # "pre.9" above "pre.13". Padded, the two orders agree.
    made = []
    version = "0.1.0-pre.1"
    for _ in range(20):
        version = vt.next_version(version)
        made.append(version)
    assert made == sorted(made), "text order must match the order they were made in"
    assert "0.1.0-pre.009" in made and "0.1.0-pre.010" in made

