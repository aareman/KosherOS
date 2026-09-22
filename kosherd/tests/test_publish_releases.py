"""Every version on master gets a release, in order, with no holes.

The releases page once read 0.2.0, 0.4.0. The 0.3.0 tick was committed, its
image built and signed, and then `gh release create` failed with a 403 that
the next run did not see. The number was spent and nothing came back for it.
The user's rule: "we should have sequential releases acc. to semver."
scripts/publish-releases.py is the release step now — it retries, and it
publishes every unreleased tick on the history, not only this run's.
"""

import importlib.util
import subprocess
import sys
from pathlib import Path
from subprocess import CompletedProcess

import pytest

ROOT = Path(__file__).parents[2]
SCRIPT = ROOT / "scripts/publish-releases.py"

spec = importlib.util.spec_from_file_location("publish_releases", SCRIPT)
pr = importlib.util.module_from_spec(spec)
sys.modules["publish_releases"] = pr
spec.loader.exec_module(pr)


def sh(*args, cwd):
    return subprocess.run(args, check=True, cwd=cwd, capture_output=True, text=True).stdout.strip()


def commit(repo, subject, version=None, extra=None):
    if version is not None:
        (repo / "VERSION").write_text(version + "\n")
    (repo / (extra or "notes.txt")).write_text(subject + "\n")
    sh("git", "add", "-A", cwd=repo)
    sh("git", "commit", "-q", "-m", subject, cwd=repo)
    return sh("git", "rev-parse", "HEAD", cwd=repo)


@pytest.fixture
def repo(tmp_path):
    """master as it looked when the gap was noticed: 0.1.0 and 0.2.0 tagged,
    0.3.0 ticked but never released, 0.4.0 the merge being released now."""
    sh("git", "init", "-q", "-b", "master", cwd=tmp_path)
    sh("git", "config", "user.email", "t@example.com", cwd=tmp_path)
    sh("git", "config", "user.name", "t", cwd=tmp_path)
    c = {}
    commit(tmp_path, "start")
    c["0.1.0"] = commit(tmp_path, "chore: version 0.1.0 [skip ci]", "0.1.0")
    sh("git", "tag", "v0.1.0", cwd=tmp_path)
    commit(tmp_path, "feat: a thing")
    c["0.2.0"] = commit(tmp_path, "chore: version 0.2.0 [skip ci]", "0.2.0")
    sh("git", "tag", "v0.2.0", cwd=tmp_path)
    commit(tmp_path, "feat(updates): channels")
    c["0.3.0"] = commit(tmp_path, "chore: version 0.3.0 [skip ci]", "0.3.0")
    commit(tmp_path, "fix(hooks): pre-push too")
    c["0.4.0"] = commit(tmp_path, "chore: version 0.4.0 [skip ci]", "0.4.0")
    return tmp_path, c


class FakeGitHub:
    """`gh` as far as the script uses it: releases exist or they don't, and
    `release create` can be told to fail a few times first."""

    def __init__(self, fail_first=0, existing=()):
        self.releases = set(existing)
        self.fail_first = fail_first
        self.created = []  # the full argument lists of every create attempt

    def __call__(self, *args):
        if args[:2] == ("release", "view"):
            code = 0 if args[2] in self.releases else 1
            return CompletedProcess(args, code, "", "" if code == 0 else "release not found")
        if args[:2] == ("release", "create"):
            self.created.append(list(args))
            if self.fail_first:
                self.fail_first -= 1
                return CompletedProcess(args, 1, "",
                                        "HTTP 403: Resource not accessible by integration")
            self.releases.add(args[2])
            return CompletedProcess(args, 0, f"https://github.com/x/y/releases/tag/{args[2]}", "")
        raise AssertionError(f"unexpected gh {args}")


@pytest.fixture
def github(monkeypatch):
    fake = FakeGitHub()
    monkeypatch.setattr(pr, "gh", fake)
    return fake


@pytest.fixture
def notes(monkeypatch):
    """release-notes.py reads the real repository; here we only need to know
    what it was asked for."""
    asked = []

    def fake_notes(version, previous, commit, image, built, image_failed):
        asked.append(dict(version=version, previous=previous, commit=commit,
                          built=built, image_failed=image_failed))
        return f"notes for {version}\n"
    monkeypatch.setattr(pr, "notes_for", fake_notes)
    return asked


def test_the_versions_left_behind_are_found_oldest_first(repo):
    path, c = repo
    assert pr.pending_versions(c["0.4.0"], path) == [(c["0.3.0"], "0.3.0"), (c["0.4.0"], "0.4.0")]


def test_only_this_history_is_considered(repo):
    # Re-running the 0.3.0 workflow must not release 0.4.0 for it.
    path, c = repo
    assert pr.pending_versions(c["0.3.0"], path) == [(c["0.3.0"], "0.3.0")]
    assert pr.pending_versions(c["0.2.0"], path) == []


def test_a_hand_edited_version_is_not_a_release(repo):
    path, c = repo
    # A person moving to 1.0.0 by hand: the merge that carries it gets the
    # release, through the tick CI makes on top.
    by_hand = commit(path, "feat!: the product is ready", "1.0.0")
    # And a tick whose subject and file disagree is not trusted either.
    odd = commit(path, "chore: version 1.0.1 [skip ci]", "1.0.2")
    pending = pr.pending_versions(odd, path)
    assert (by_hand, "1.0.0") not in pending
    assert all(v != "1.0.1" for _, v in pending)
    assert pending[:2] == [(c["0.3.0"], "0.3.0"), (c["0.4.0"], "0.4.0")]


def test_the_old_pre_release_ticks_are_never_backfilled(repo):
    path, c = repo
    legacy = commit(path, "chore: version 0.1.0-pre.072 [skip ci]", "0.1.0-pre.072")
    assert all(v != "0.1.0-pre.072" for _, v in pr.pending_versions(legacy, path))


def test_the_previous_version_is_found_by_history_not_by_date(repo):
    path, c = repo
    assert pr.previous_tag(c["0.3.0"], path) == "v0.2.0"
    assert pr.previous_tag(c["0.1.0"], path) == ""


def test_the_gap_is_closed_before_this_version_is_released(repo, github, notes, monkeypatch):
    path, c = repo
    monkeypatch.setattr(pr, "image_exists", lambda image, tag: tag == "v0.3.0")
    made = pr.publish(c["0.4.0"], "ghcr.io/x/y", "yes", cwd=path, sleep=lambda s: None)
    assert made == ["v0.3.0", "v0.4.0"]
    first, second = github.created
    assert first[2] == "v0.3.0" and first[first.index("--target") + 1] == c["0.3.0"]
    assert second[2] == "v0.4.0" and second[second.index("--target") + 1] == c["0.4.0"]
    # The backfilled release must not take the Latest badge off the newest.
    assert "--latest=false" in first
    assert "--latest=false" not in second
    assert "KosherOS 0.3.0" in first and "KosherOS 0.4.0" in second
    # Its notes cover exactly its own span, and say the image exists because
    # the registry says so — this run only knows about its own image.
    assert notes[0] == dict(version="0.3.0", previous="v0.2.0", commit=c["0.3.0"],
                            built="yes", image_failed=False)
    # And the newest release's "since" is the one just filled in.
    assert notes[1] == dict(version="0.4.0", previous="v0.3.0", commit=c["0.4.0"],
                            built="yes", image_failed=False)
    assert {"v0.3.0", "v0.4.0"} <= pr.existing_tags(path)


def test_a_backfilled_version_with_no_image_says_so(repo, github, notes, monkeypatch):
    path, c = repo
    monkeypatch.setattr(pr, "image_exists", lambda image, tag: False)
    pr.publish(c["0.4.0"], "ghcr.io/x/y", "no", image_failed=True, cwd=path, sleep=lambda s: None)
    assert notes[0]["built"] == "no" and notes[0]["image_failed"] is False
    # This run's own failure is reported for this run's version only.
    assert notes[1]["built"] == "no" and notes[1]["image_failed"] is True


def test_the_release_call_is_retried(repo, github, notes, monkeypatch):
    # The 0.3.0 gap was one 403 that the very next run did not see.
    path, c = repo
    github.fail_first = 1
    monkeypatch.setattr(pr, "image_exists", lambda image, tag: True)
    slept = []
    made = pr.publish(c["0.3.0"], "ghcr.io/x/y", "yes", cwd=path, sleep=slept.append)
    assert made == ["v0.3.0"]
    assert len(github.created) == 2
    assert slept == [pr.BACKOFF]


def test_it_gives_up_eventually_and_says_so(repo, github, notes, monkeypatch):
    path, c = repo
    github.fail_first = 99
    slept = []
    with pytest.raises(SystemExit) as raised:
        pr.publish(c["0.3.0"], "ghcr.io/x/y", "yes", cwd=path, sleep=slept.append)
    assert "v0.3.0" in str(raised.value) and str(pr.ATTEMPTS) in str(raised.value)
    assert len(github.created) == pr.ATTEMPTS
    assert len(slept) == pr.ATTEMPTS - 1


def test_a_release_another_run_made_first_is_left_alone(repo, github, notes, monkeypatch):
    # Two merges close together: both runs see the same gap. The second's
    # create fails because the first got there; that is not an error.
    path, c = repo

    class Racing(FakeGitHub):
        def __call__(self, *args):
            if args[:2] == ("release", "create") and args[2] == "v0.3.0" and not self.created:
                self.created.append(list(args))
                self.releases.add("v0.3.0")
                return CompletedProcess(args, 1, "", "release already exists")
            return super().__call__(*args)
    racing = Racing()
    monkeypatch.setattr(pr, "gh", racing)
    made = pr.publish(c["0.4.0"], "ghcr.io/x/y", "yes", cwd=path, sleep=lambda s: None)
    assert made == ["v0.3.0", "v0.4.0"]
    assert [a[2] for a in racing.created] == ["v0.3.0", "v0.4.0"]


def test_an_existing_release_without_a_local_tag_is_skipped(repo, github, notes):
    # A fresh checkout whose tags were not fetched for a release that exists.
    path, c = repo
    github.releases.add("v0.3.0")
    made = pr.publish(c["0.4.0"], "ghcr.io/x/y", "yes", cwd=path, sleep=lambda s: None)
    assert made == ["v0.4.0"]
    assert [a[2] for a in github.created] == ["v0.4.0"]
    assert notes[0]["previous"] == "v0.3.0", "the skipped one still counts as the previous"


def test_nothing_to_do_when_every_version_is_released(repo, github, notes):
    path, c = repo
    sh("git", "tag", "v0.3.0", c["0.3.0"], cwd=path)
    sh("git", "tag", "v0.4.0", c["0.4.0"], cwd=path)
    assert pr.publish(c["0.4.0"], "ghcr.io/x/y", "yes", cwd=path) == []
    assert github.created == [] and notes == []


def test_the_real_notes_script_is_what_writes_the_notes():
    text = SCRIPT.read_text()
    assert 'NOTES = ROOT / "scripts/release-notes.py"' in text
    assert "--image-failed" in text and "--built" in text


def test_the_workflow_release_step_is_this_script():
    """The release job hands everything to the script: the sha this run
    built, whether it built an image, and whether that failed."""
    ci = (ROOT / ".github/workflows/ci.yml").read_text()
    assert "scripts/publish-releases.py" in ci
    assert '--sha "$SHA"' in ci and '--built "$built"' in ci and "$failed" in ci
    assert "git fetch --tags" in ci, "the gap is found by the tags that exist"
    assert 'gh release create' not in ci, "one place makes releases, and it retries"
