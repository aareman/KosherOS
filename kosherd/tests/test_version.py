"""The tag is the version, and the version is plain semver.

The user builds ISOs and VMs often and needs to know which build a bug came
from, so every merge to master gets its own number: `0.x.x`, a `feat` moves
the minor, anything else the patch, and never a major: "Don't release a major
release until i say so." Nothing is committed to carry the number — it is
counted from the last tag, and the tag is pushed last, once the build has
passed. The VERSION file that CI used to commit first spent a number before
anything downstream had run, and four numbers were lost that way.
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
def test_what_a_change_asks_for(subjects, expected):
    assert vt.kind_of(subjects) == expected


def test_a_feature_in_the_body_is_not_a_feature():
    # Only the subject's own type counts: a fix that mentions the word in
    # passing is still a fix.
    assert vt.kind_of(["fix: stop the feat: prefix being read from prose"]) == "patch"


# --- from the tags -----------------------------------------------------------

def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


def _commit(repo: Path, message: str) -> str:
    (repo / "a.txt").write_text(message)
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _repo(tmp_path: Path, tag: str | None = "v0.4.2") -> Path:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    _git(tmp_path, "config", "user.email", "t@example")
    _git(tmp_path, "config", "user.name", "t")
    _commit(tmp_path, "chore: start")
    if tag:
        _git(tmp_path, "tag", tag)
    return tmp_path


def test_a_tagged_commit_is_its_version(tmp_path):
    repo = _repo(tmp_path)
    assert vt.release_version("HEAD", repo) == "0.4.2"
    assert vt.dev_version("HEAD", repo) == "0.4.2", "a tagged build is a release wherever it is built"


def test_a_fix_after_the_tag_is_the_next_patch(tmp_path):
    repo = _repo(tmp_path)
    sha = _commit(repo, "fix: something small")
    assert vt.release_version("HEAD", repo) == "0.4.3"
    # Built anywhere but the release step, the same number says it is not
    # a release: how far past the tag, and which commit.
    assert vt.dev_version("HEAD", repo) == f"0.4.3-dev.1+g{sha[:7]}"


def test_a_feature_in_the_changes_moves_the_minor(tmp_path):
    repo = _repo(tmp_path)
    _commit(repo, "fix: something small")
    _commit(repo, "feat(store): an Installing shelf")
    assert vt.release_version("HEAD", repo) == "0.5.0"
    assert vt.kind_since(vt.last_tag("HEAD", repo), "HEAD", repo) == "minor"


def test_only_what_came_after_the_last_tag_counts(tmp_path):
    # The feature is behind the tag: it was numbered already.
    repo = _repo(tmp_path)
    _commit(repo, "feat: shipped last time")
    _git(repo, "tag", "v0.5.0")
    _commit(repo, "docs: a paragraph")
    assert vt.release_version("HEAD", repo) == "0.5.1"


def test_any_commit_can_be_asked_not_only_head(tmp_path):
    repo = _repo(tmp_path)
    first = _commit(repo, "feat: one")
    _commit(repo, "fix: two")
    assert vt.release_version(first, repo) == "0.5.0"
    assert vt.release_version("HEAD", repo) == "0.5.0", "both would be released as the same number"


def test_the_old_series_is_stepped_off_from_its_tag(tmp_path):
    repo = _repo(tmp_path, tag="v0.1.0-pre.071")
    _commit(repo, "feat: the last of the pre-releases")
    assert vt.release_version("HEAD", repo) == "0.1.0"


def test_a_moving_tag_is_not_a_version(tmp_path):
    # `stable` (or anything else a person pins with a tag) must not be
    # mistaken for the last version and break the count.
    repo = _repo(tmp_path)
    _commit(repo, "fix: one")
    _git(repo, "tag", "stable")
    _commit(repo, "fix: two")
    assert vt.last_tag("HEAD", repo) == "v0.4.2"
    assert vt.release_version("HEAD", repo) == "0.4.3", "two fixes since the tag are one patch step"


def test_without_a_tag_the_release_version_refuses_and_the_dev_version_is_honest(tmp_path):
    # A shallow clone has no tags; guessing a release number there would
    # be wrong, and a local build should at least not claim one.
    repo = _repo(tmp_path, tag=None)
    sha = _commit(repo, "fix: one")
    with pytest.raises(ValueError) as raised:
        vt.release_version("HEAD", repo)
    assert "fetch" in str(raised.value)
    assert vt.dev_version("HEAD", repo) == f"0.0.0-dev.2+g{sha[:7]}"


def test_outside_a_repository_the_dev_version_still_answers(tmp_path):
    assert vt.dev_version("HEAD", tmp_path) == "0.0.0-dev.0"


def test_the_command_line(tmp_path, monkeypatch, capsys):
    repo = _repo(tmp_path)
    sha = _commit(repo, "feat: something worth a minor")
    monkeypatch.setattr(vt, "ROOT", repo)
    for command in ("show", "release", "kind", "tag"):
        assert vt.main([command]) == 0
    assert capsys.readouterr().out.split() == [
        f"0.5.0-dev.1+g{sha[:7]}", "0.5.0", "minor", "v0.5.0"]
    assert vt.main(["nonsense"]) == 2
    _git(repo, "tag", "-d", "v0.4.2")
    assert vt.main(["release"]) == 1, "no tag: a plain failure, not a traceback"
    assert "fetch" in capsys.readouterr().err


# --- nothing commits a version any more ---------------------------------------

def test_there_is_no_version_file():
    """The number is passed to the build, never committed.

    Committing it first is what spent a number before the build had passed.
    The Containerfile takes it as KOSHER_VERSION and writes it into the image
    itself; `just build` and the image job pass what scripts/version.py says.
    """
    assert not (ROOT / "VERSION").exists()
    containerfile = (ROOT / "os-image/Containerfile").read_text()
    assert "COPY VERSION" not in containerfile
    assert '"$KOSHER_VERSION" > /usr/share/kosher/VERSION' in containerfile
    assert 'KOSHER_VERSION=$(python3 scripts/version.py show)' in (ROOT / "Justfile").read_text()
    ci = (ROOT / ".github/workflows/ci.yml").read_text()
    assert 'version="$(python3 scripts/version.py release)"' in ci
    assert '--build-arg "KOSHER_VERSION=$version"' in ci


def test_ci_commits_no_version_tick():
    ci = (ROOT / ".github/workflows/ci.yml").read_text()
    assert "version.py bump" not in ci
    assert "[skip ci]" not in ci and "HEAD:master" not in ci, "CI pushes no commits to master"
    assert "--prerelease" not in ci, "0.x.x builds are releases, not pre-releases"


def test_the_dev_shell_installs_no_version_hook():
    nix = (ROOT / "devenv.nix").read_text()
    assert "git-hooks.hooks.version-bump" not in nix
    assert "version.py bump" not in nix


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
