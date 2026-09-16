"""Every merge carries its own version, and the version is plain semver.

The user builds ISOs and VMs often and needs to know which build a bug came
from, so CI writes a new version for every push to master. What it writes
is `0.x.x` — a `feat` moves the minor, anything else the patch — and never
a major: "Don't release a major release until i say so."
"""

import importlib.util
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


@pytest.mark.parametrize("current,kind,expected", [
    ("0.4.2", "patch", "0.4.3"),
    ("0.4.2", "minor", "0.5.0"),
    ("0.4.9", "minor", "0.5.0"),
    ("0.9.0", "minor", "0.10.0"),   # ten follows nine; nothing is padded
    ("0.4.2\n", "patch", "0.4.3"),
    ("1.0", "patch", "1.0.1"),      # the old two-part form is read as X.Y.0
])
def test_the_next_version(current, kind, expected):
    assert vt.next_version(current, kind) == expected


def test_a_patch_is_the_default():
    assert vt.next_version("0.4.2") == "0.4.3"


def test_the_major_never_moves_on_its_own():
    # "Don't release a major release until i say so." Not a rounding
    # decision the machine gets to make.
    with pytest.raises(ValueError) as raised:
        vt.next_version("0.9.9", "major")
    assert "decision" in str(raised.value)
    # And no ordinary step can reach it, however far the minor has run.
    assert vt.next_version("0.999.0", "minor") == "0.1000.0"
    assert vt.next_version("0.999.9", "patch") == "0.999.10"


def test_the_old_pre_release_series_is_stepped_off_once():
    # 0.1.0-pre.N was a release that never arrived. Dropping the suffix is
    # the release it was leading to; after that the ordinary rules apply.
    assert vt.next_version("0.1.0-pre.071", "patch") == "0.1.0"
    assert vt.next_version("0.1.0-pre.071", "minor") == "0.1.0"
    assert vt.next_version("0.1.0", "patch") == "0.1.1"


@pytest.mark.parametrize("bad", ["", "0.1.0-rc.1", "v0.1.0", "0.1.0-pre", "banana"])
def test_anything_else_is_refused(bad):
    with pytest.raises(ValueError):
        vt.next_version(bad)


@pytest.mark.parametrize("subjects,expected", [
    (["fix(youtube): the category is read from the microformat"], "patch"),
    (["chore: version 0.4.2 [skip ci]"], "patch"),
    (["docs: the overview mentions groups"], "patch"),
    (["feat(admin): reset a supervised account's password"], "minor"),
    (["fix: a thing", "feat: another thing"], "minor"),
    (["feat!: the policy file moved"], "minor"),
    (["refactor!: the client's signatures changed"], "minor"),
    (["fix: a thing\n\nBREAKING CHANGE: the rules file is keyed by uid"], "minor"),
    ([], "patch"),
    (["", "   "], "patch"),
])
def test_what_a_merge_asks_for(subjects, expected):
    assert vt.kind_of(subjects) == expected


def test_a_feature_in_the_body_is_not_a_feature():
    # Only the subject's own type counts: a fix that mentions the word in
    # passing is still a fix.
    assert vt.kind_of(["fix: stop the feat: prefix being read from prose"]) == "patch"


def _repo(tmp_path: Path, version="0.4.2") -> Path:
    def git(*args):
        subprocess.run(["git", "-C", str(tmp_path), *args], check=True,
                       capture_output=True)

    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    git("config", "user.email", "t@example")
    git("config", "user.name", "t")
    (tmp_path / "VERSION").write_text(version + "\n")
    git("add", "VERSION")
    git("commit", "-q", "-m", "chore: start")
    git("tag", "v" + version)
    return tmp_path


def _commit(repo: Path, message: str) -> None:
    (repo / "a.txt").write_text(message)
    subprocess.run(["git", "-C", str(repo), "add", "a.txt"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", message], check=True)


def test_bump_reads_the_merge_and_writes_the_new_version(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    monkeypatch.delenv("KOSHER_NO_BUMP", raising=False)
    _commit(repo, "fix: something small")
    assert vt.bump(repo / "VERSION") == "0.4.3"
    assert (repo / "VERSION").read_text() == "0.4.3\n"
    staged = subprocess.run(["git", "-C", str(repo), "diff", "--cached", "--name-only"],
                            capture_output=True, text=True, check=True).stdout.split()
    assert staged == ["VERSION"], "the bump must land in the commit being made"


def test_a_feature_in_the_merge_moves_the_minor(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    monkeypatch.delenv("KOSHER_NO_BUMP", raising=False)
    _commit(repo, "fix: something small")
    _commit(repo, "feat(store): an Installing shelf")
    assert vt.bump(repo / "VERSION") == "0.5.0"


def test_only_what_came_after_the_last_version_counts(tmp_path, monkeypatch):
    # The feature is behind the tag: it was numbered already.
    repo = _repo(tmp_path)
    monkeypatch.delenv("KOSHER_NO_BUMP", raising=False)
    _commit(repo, "feat: shipped last time")
    subprocess.run(["git", "-C", str(repo), "tag", "v0.5.0"], check=True)
    (repo / "VERSION").write_text("0.5.0\n")
    _commit(repo, "docs: a paragraph")
    assert vt.bump(repo / "VERSION") == "0.5.1"


def test_a_merge_commit_is_not_bumped(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    monkeypatch.delenv("KOSHER_NO_BUMP", raising=False)
    (repo / ".git/MERGE_HEAD").write_text("0" * 40 + "\n")
    assert vt.bump(repo / "VERSION") is None
    assert (repo / "VERSION").read_text() == "0.4.2\n"


def test_the_bump_can_be_switched_off_for_one_commit(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    monkeypatch.setenv("KOSHER_NO_BUMP", "1")
    assert vt.bump(repo / "VERSION") is None


def test_the_repo_version_is_plain_semver():
    major, minor, patch, pre = vt.parse((ROOT / "VERSION").read_text())
    assert major == 0, "1.0.0 is the user's call, not a merge's"
    assert isinstance(minor, int) and isinstance(patch, int)


def test_ci_numbers_every_merge_and_releases_it_as_a_release():
    ci = (ROOT / ".github/workflows/ci.yml").read_text()
    assert "scripts/version.py bump" in ci
    assert "--prerelease" not in ci, "0.x.x builds are releases, not pre-releases"


def test_the_dev_shell_installs_no_version_hook_any_more():
    """The tick moved from every commit to every merge.

    The pre-commit hook numbered every commit on every branch, which the
    maintainer called excessive; CI's version job now writes VERSION once
    per push to master.
    """
    nix = (ROOT / "devenv.nix").read_text()
    assert "git-hooks.hooks.version-bump" not in nix


def test_nothing_tells_a_person_their_build_is_a_pre_release():
    # What a reader is told. (build-docs.py still knows the old tag shape,
    # because the releases page lists the builds that carried it.)
    for rel in ("readme.md", "docs/deployment.md", ".github/workflows/ci.yml",
                ".github/workflows/release-stable.yml", "Justfile"):
        text = (ROOT / rel).read_text()
        assert "pre-release" not in text.lower(), rel
        assert "-pre." not in text, rel
    page = (ROOT / "scripts/build-docs.py").read_text()
    assert "becomes a release on the **edge** channel" in page


def test_the_command_line(tmp_path, monkeypatch, capsys):
    repo = _repo(tmp_path, "0.4.2")
    _commit(repo, "feat: something worth a minor")
    monkeypatch.setattr(vt, "VERSION_FILE", repo / "VERSION")
    assert vt.main(["show"]) == 0
    assert vt.main(["next"]) == 0
    assert vt.main(["kind"]) == 0
    assert capsys.readouterr().out.split() == ["0.4.2", "0.5.0", "minor"]
    assert vt.main(["nonsense"]) == 2
