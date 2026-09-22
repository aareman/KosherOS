"""Every merge that passes gets a tag and a release, in order, with no holes.

The tag is pushed LAST, once the build has passed, so a number is spent only
when its release is about to exist; whatever fails before that spends
nothing, and whatever fails after it is finished by the next run. The
releases page once read 0.2.0, 0.4.0, 0.4.1 — then 0.6.0 in the tree with
nothing released since 0.4.1 — because the old design committed the number
first and released an explicit commit, which GitHub refuses from Actions
whenever a workflow file changed after that commit. The user's rule: "we
should have sequential releases acc. to semver."
"""

import importlib.util
import json
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


def commit(repo, subject):
    (repo / "notes.txt").write_text(subject + "\n")
    sh("git", "add", "notes.txt", cwd=repo)
    sh("git", "commit", "-q", "-m", subject, cwd=repo)
    return sh("git", "rev-parse", "HEAD", cwd=repo)


def remote_tags(repo) -> dict[str, str]:
    """tag -> commit as the REMOTE has them, which is what counts."""
    listing = sh("git", "ls-remote", "--tags", "origin", cwd=repo)
    found = {}
    for line in listing.splitlines():
        sha, _, ref = line.partition("\t")
        if not ref.endswith("^{}"):
            found[ref.removeprefix("refs/tags/")] = sha
    return found


@pytest.fixture
def repo(tmp_path):
    """A clone with a bare origin, at v0.6.0 with one fix on top: master as
    a merge lands, before the release job has run."""
    work = tmp_path / "work"
    work.mkdir()
    sh("git", "init", "-q", "-b", "master", cwd=work)
    sh("git", "config", "user.email", "t@example.com", cwd=work)
    sh("git", "config", "user.name", "t", cwd=work)
    commit(work, "start")
    tagged = commit(work, "feat: the first feature")
    sh("git", "tag", "v0.6.0", cwd=work)
    origin = tmp_path / "origin.git"
    sh("git", "clone", "-q", "--bare", str(work), str(origin), cwd=tmp_path)
    sh("git", "remote", "add", "origin", str(origin), cwd=work)
    head = commit(work, "fix(hooks): pre-push too")
    return work, {"v0.6.0": tagged, "head": head}


class FakeGitHub:
    """`gh` as far as the script uses it: releases exist or they don't, and
    `release create` can be told to fail a few times first."""

    def __init__(self):
        self.releases = {"v0.6.0"}  # the fixture's last version was released
        self.fail_first = 0
        self.created = []  # the full argument lists of every create attempt

    def __call__(self, *args):
        if args[:2] == ("release", "list"):
            body = json.dumps([{"tagName": t} for t in sorted(self.releases)])
            return CompletedProcess(args, 0, body, "")
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


class FakeRegistry:
    """What the registry holds, by tag, and what was copied into it."""

    def __init__(self, *tags):
        self.tags = set(tags)
        self.copied = []

    def exists(self, image, tag):
        return tag in self.tags

    def copy(self, image, source, tag):
        self.copied.append((source, tag))
        self.tags.add(tag)


@pytest.fixture
def github(monkeypatch):
    fake = FakeGitHub()
    monkeypatch.setattr(pr, "gh", fake)
    return fake


@pytest.fixture
def registry(monkeypatch, repo):
    _, c = repo
    fake = FakeRegistry(c["head"][:12])  # the image job pushed this run's image by sha
    monkeypatch.setattr(pr, "image_exists", fake.exists)
    monkeypatch.setattr(pr, "tag_image", fake.copy)
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


def publish(repo, sha, **kwargs):
    return pr.publish(sha, "ghcr.io/x/y", cwd=repo, sleep=lambda s: None, **kwargs)


def test_the_tag_lands_first_then_the_image_then_the_release(repo, github, registry, notes):
    path, c = repo
    assert publish(path, c["head"]) == ["v0.6.1"]
    # The tag is on the remote, at this commit: the number is spent.
    assert remote_tags(path)["v0.6.1"] == c["head"]
    # The sha-tagged image the build pushed was copied to the version's tag.
    assert registry.copied == [(c["head"][:12], "v0.6.1")]
    # The release is made on the tag that exists — no target commit, which
    # is the argument GitHub refuses from Actions — and it is the latest.
    [create] = github.created
    assert create[2] == "v0.6.1" and "--target" not in create and "--latest=false" not in create
    assert "KosherOS 0.6.1" in create
    assert notes == [dict(version="0.6.1", previous="v0.6.0", commit=c["head"],
                          built="yes", image_failed=False)]


def test_a_feature_moves_the_minor(repo, github, registry, notes):
    path, c = repo
    head = commit(path, "feat(store): the whole store per account")
    registry.tags.add(head[:12])
    assert publish(path, head) == ["v0.7.0"]
    assert remote_tags(path)["v0.7.0"] == head


def test_a_merge_with_no_image_says_so(repo, github, registry, notes):
    path, c = repo
    registry.tags.clear()  # the image job was skipped: docs only
    publish(path, c["head"])
    assert registry.copied == []
    assert notes[0]["built"] == "no" and notes[0]["image_failed"] is False


def test_a_failed_image_build_is_reported_for_this_run_only(repo, github, registry, notes):
    path, c = repo
    registry.tags.clear()
    publish(path, c["head"], image_failed=True)
    assert notes[0]["image_failed"] is True


def test_running_again_does_nothing_twice(repo, github, registry, notes):
    path, c = repo
    publish(path, c["head"])
    assert publish(path, c["head"]) == []
    assert len(github.created) == 1 and len(registry.copied) == 1


def test_a_number_taken_meanwhile_is_counted_from(repo, github, registry, notes):
    """Two merges close together both work out 0.6.1. The first to push
    the tag has it; the second, whose commit CONTAINS the first, counts
    again from that tag and becomes 0.6.2."""
    path, c = repo
    first = c["head"]
    second = commit(path, "fix: right behind it")
    registry.tags.add(second[:12])
    # The other run tagged `first` while this one was not looking.
    sh("git", "push", "-q", "origin", f"{first}:refs/tags/v0.6.1", cwd=path)
    github.releases.add("v0.6.1")
    assert publish(path, second) == ["v0.6.2"]
    assert remote_tags(path)["v0.6.2"] == second
    assert notes[0]["previous"] == "v0.6.1"


def test_a_merge_released_inside_the_next_one_gets_no_release_of_its_own(repo, github, registry, notes):
    """The reverse race: the LATER merge tagged 0.6.1 first, on a commit
    that contains ours. Our changes are in that release; making another
    would number the same changes twice."""
    path, c = repo
    first = c["head"]
    second = commit(path, "fix: right behind it")
    sh("git", "push", "-q", "origin", f"{second}:refs/tags/v0.6.1", cwd=path)
    assert publish(path, first) == []
    assert github.created == []
    assert set(remote_tags(path)) == {"v0.6.0", "v0.6.1"}


def test_a_version_tag_on_an_unrelated_commit_is_an_error(repo, github, registry, notes):
    path, c = repo
    sh("git", "checkout", "-q", "--orphan", "elsewhere", cwd=path)
    stray = commit(path, "fix: from another history")
    sh("git", "checkout", "-q", "master", cwd=path)
    sh("git", "push", "-q", "origin", f"{stray}:refs/tags/v0.6.1", cwd=path)
    with pytest.raises(SystemExit) as raised:
        publish(path, c["head"])
    assert "unrelated" in str(raised.value)


def test_tags_without_a_release_are_finished_oldest_first_and_not_marked_latest(
        repo, github, registry, notes):
    """A run that died between the tag and the release, or tags a person
    pushed by hand to close a gap: the next run gives each its release —
    its image too, when the build for that commit is in the registry — and
    leaves the Latest badge on the newest."""
    path, c = repo
    older = sh("git", "rev-parse", "HEAD~2", cwd=path)  # "start"
    sh("git", "push", "-q", "origin", f"{older}:refs/tags/v0.5.0", cwd=path)
    github.releases.discard("v0.6.0")  # tagged, never released either
    registry.tags.add(c["v0.6.0"][:12])  # 0.6.0 was built; 0.5.0 never was
    made = publish(path, c["head"])
    assert made == ["v0.6.1", "v0.5.0", "v0.6.0"]
    by_tag = {a[2]: a for a in github.created}
    assert "--latest=false" not in by_tag["v0.6.1"]
    assert "--latest=false" in by_tag["v0.5.0"] and "--latest=false" in by_tag["v0.6.0"]
    assert (c["v0.6.0"][:12], "v0.6.0") in registry.copied
    asked = {n["version"]: n for n in notes}
    assert asked["0.6.0"] == dict(version="0.6.0", previous="v0.5.0", commit=c["v0.6.0"],
                                  built="yes", image_failed=False)
    assert asked["0.5.0"]["built"] == "no"


def test_a_newer_tag_is_not_finished_by_an_older_run(repo, github, registry, notes):
    # Re-running the 0.6.0 workflow must not make 0.6.1's release for it.
    path, c = repo
    github.releases.discard("v0.6.0")  # this re-run's own release is missing
    sh("git", "push", "-q", "origin", f"{c['head']}:refs/tags/v0.6.1", cwd=path)
    assert publish(path, c["v0.6.0"]) == ["v0.6.0"]
    assert [a[2] for a in github.created] == ["v0.6.0"]


def test_the_release_call_is_retried(repo, github, registry, notes):
    path, c = repo
    github.fail_first = 1
    slept = []
    made = pr.publish(c["head"], "ghcr.io/x/y", cwd=path, sleep=slept.append)
    assert made == ["v0.6.1"]
    assert len(github.created) == 2
    assert slept == [pr.BACKOFF]


def test_it_gives_up_eventually_and_the_next_run_finishes_the_job(repo, github, registry, notes):
    path, c = repo
    github.fail_first = 99
    with pytest.raises(SystemExit) as raised:
        publish(path, c["head"])
    assert "v0.6.1" in str(raised.value) and str(pr.ATTEMPTS) in str(raised.value)
    # The number is spent — the tag is there — so the next run finishes it
    # rather than taking a new one.
    assert remote_tags(path)["v0.6.1"] == c["head"]
    github.fail_first = 0
    assert publish(path, c["head"]) == ["v0.6.1"]
    assert remote_tags(path).keys() == {"v0.6.0", "v0.6.1"}


def test_a_release_another_run_made_first_is_left_alone(repo, registry, notes, monkeypatch):
    path, c = repo

    class Racing(FakeGitHub):
        def __call__(self, *args):
            if args[:2] == ("release", "create") and not self.created:
                self.created.append(list(args))
                self.releases.add(args[2])
                return CompletedProcess(args, 1, "", "release already exists")
            return super().__call__(*args)
    racing = Racing()
    monkeypatch.setattr(pr, "gh", racing)
    assert publish(path, c["head"]) == ["v0.6.1"]
    assert len(racing.created) == 1


def test_the_real_notes_script_is_what_writes_the_notes():
    text = SCRIPT.read_text()
    assert 'NOTES = ROOT / "scripts/release-notes.py"' in text
    assert '"--notes-file", str(notes)' in text
    assert "--image-failed" in text and "--built" in text


def test_the_workflow_release_step_is_this_script():
    """The release job hands the commit it built to the script, which pushes
    the tag with git and releases the tag — never a target commit."""
    ci = (ROOT / ".github/workflows/ci.yml").read_text()
    assert "scripts/publish-releases.py" in ci
    assert '--sha "$GITHUB_SHA"' in ci and "$failed" in ci
    assert "gh release create" not in ci, "one place makes releases, and it retries"
    assert "--target" not in ci
    script = SCRIPT.read_text()
    assert '"push", "--quiet", "origin", f"{commit}:refs/tags/{tag}"' in script
    assert "--target" not in script
    # The image's version tag and its signature are made here too, after
    # the tag: skopeo and cosign are on the job.
    assert "skopeo" in ci and "cosign-installer" in ci
    assert '"skopeo", "copy", "--all"' in script and '"cosign", "sign", "--yes"' in script
