"""The GitHub workflows must at least be valid YAML.

A workflow that does not parse fails the run before any job starts, with no
log to read: three pushes went "red" that way with nothing to show for it,
because a heredoc inside a run block put lines at column zero and ended the
block scalar. Parsing them here catches that in `just test`.
"""

from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

ROOT = Path(__file__).parents[2]
WORKFLOWS = sorted((ROOT / ".github/workflows").glob("*.yml"))


@pytest.mark.parametrize("path", WORKFLOWS, ids=[p.name for p in WORKFLOWS])
def test_the_workflow_parses(path):
    document = yaml.safe_load(path.read_text())
    assert isinstance(document, dict)
    assert document.get("jobs"), f"{path.name} defines no jobs"
    for name, job in document["jobs"].items():
        # A job either runs somewhere or calls another workflow that does.
        assert "runs-on" in job or "uses" in job, f"{path.name}: {name} has no runner"


def test_no_run_block_smuggles_a_heredoc():
    # A heredoc body is written at the indentation of the file, not of the
    # block scalar, so it silently ends the block. Build text with echo
    # into a file instead.
    for path in WORKFLOWS:
        for line in path.read_text().splitlines():
            assert "<<" not in line or "<<-" in line, f"{path.name}: {line.strip()[:70]}"


def test_ci_publishes_edge_and_never_stable():
    ci = (ROOT / ".github/workflows/ci.yml").read_text()
    assert "$IMAGE:edge" in ci
    assert "$IMAGE:stable" not in ci, "stable is promoted by hand, never by a push"
    promote = (ROOT / ".github/workflows/release-stable.yml").read_text()
    assert "workflow_dispatch" in promote and "$IMAGE:stable" in promote


def test_a_pull_requests_image_is_built_only_on_request_and_never_as_a_channel():
    """Pull requests build no image unless asked (#90): the `try-image`
    label builds the branch as :pr-<number>, signed with the key the
    machines check, and leaves the channels and the versions alone."""
    path = ROOT / ".github/workflows/try-image.yml"
    text = path.read_text()
    document = yaml.safe_load(text)
    triggers = document.get(True) or document.get("on")
    assert set(triggers) == {"pull_request"}, "only a pull request event, never a push"
    job = document["jobs"]["image"]
    condition = " ".join(job["if"].split())
    assert "'try-image'" in condition, "the label is the request"
    assert "head.repo.full_name == github.repository" in condition, \
        "a fork has no secrets; its build would fail at the push"
    assert "pr-${{ github.event.pull_request.number }}" in text
    assert "$IMAGE:edge" not in text and "$IMAGE:stable" not in text
    assert "scripts/version.py release" in text and "-pr.${PR}" in text
    assert job["steps"][0]["with"]["fetch-depth"] == 0, "the version is counted from the tags"
    assert job["steps"][0]["with"]["ref"] == "${{ github.event.pull_request.head.sha }}"
    assert "COSIGN_PRIVATE_KEY" in text and "cosign sign --yes --key" in text, \
        "unsigned, no machine could take it"
    assert document["concurrency"]["cancel-in-progress"] is True
    # And the main workflow still skips pull requests on its own.
    ci = (ROOT / ".github/workflows/ci.yml").read_text()
    assert "a pull request: the image is built from the merge, not here" in ci


def test_every_version_that_passes_gets_a_release():
    """A commit whose tests pass must produce a release, image or not.

    The image job is skipped for a commit that changes nothing in the image
    (docs, tests, CI), and it used to carry the release with it — so those
    versions existed in VERSION and in no history anywhere. The release is
    its own job now, and runs when the tests pass and the image did not
    fail.
    """
    document = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    release = document["jobs"]["release"]
    assert "image" in release["needs"], "it must wait for the image, when there is one"
    condition = " ".join(release["if"].split())
    assert "always()" in condition, "or a skipped image would skip the release"
    for job in ("unit", "compile", "shell"):
        assert f"needs.{job}.result == 'success'" in condition, job
    assert "needs.image.result != 'cancelled'" in condition
    assert "github.event_name == 'push'" in condition
    # A failed image no longer blocks the release: the version is real, its
    # changes are recorded, and the notes say no image exists for it.
    assert "needs.image.result != 'failure'" not in condition


def test_what_changed_is_counted_from_the_last_version_tag():
    """A merge whose run failed got no tag and no build; its image changes
    are still waiting for the next run. Diffing only the push once built
    nothing for a shellcheck fix while the developer-tooling stack and a
    new wallpaper, merged just before with failing runs, sat unbuilt, and
    that release said "no new image" (#103)."""
    ci = (ROOT / ".github/workflows/ci.yml").read_text()
    assert "git describe --tags --abbrev=0 --match 'v*'" in ci
    assert 'git diff --name-only "$since" "$GITHUB_SHA"' in ci
    assert 'git diff --name-only "$BASE" "$GITHUB_SHA"' not in ci


def test_the_image_push_is_retried():
    # One 502 from the registry, mid-layer, cost a release its image (#103).
    ci = (ROOT / ".github/workflows/ci.yml").read_text()
    assert 'retry podman push "$IMAGE:${SHA::12}"' in ci
    assert 'retry podman push "$IMAGE:edge"' in ci
    assert 'retry cosign sign --yes "$IMAGE:edge"' in ci
    assert ci.count("for n in 1 2 3 4 5; do") == 2


def test_the_release_says_when_there_is_no_new_image():
    # The wording lives in scripts/release-notes.py now; the workflow only
    # tells it which case this is (see test_release_notes.py).
    ci = (ROOT / ".github/workflows/ci.yml").read_text()
    assert "scripts/publish-releases.py" in ci
    assert "--image-failed" in ci
    publish = (ROOT / "scripts/publish-releases.py").read_text()
    assert "scripts/release-notes.py" in publish, "which hands the wording to release-notes.py"
    # Whether an image exists is asked of the registry, not of this run:
    # a release finished by a later run knows nothing of the build.
    assert '"skopeo", "inspect", "--raw"' in publish


def test_the_version_tag_on_the_image_is_made_after_the_version_is_settled():
    """The image job pushes the sha tag and :edge; the version's tag is a
    server-side copy made by the release job once the git tag has landed,
    signed like the rest. Before, the image job tagged :vX from a VERSION
    file — a number that could turn out never to be released."""
    ci = (ROOT / ".github/workflows/ci.yml").read_text()
    assert 'podman push "$IMAGE:${SHA::12}"' in ci and 'podman push "$IMAGE:edge"' in ci
    assert "$IMAGE:v$version" not in ci
    publish = (ROOT / "scripts/publish-releases.py").read_text()
    assert '"skopeo", "copy", "--all"' in publish
    assert '"cosign", "sign", "--yes"' in publish
    document = yaml.safe_load(ci)
    release = document["jobs"]["release"]
    assert release["permissions"]["packages"] == "write"
    assert release["permissions"]["id-token"] == "write", "keyless signing needs it"
    assert any("cosign-installer" in str(step.get("uses")) for step in release["steps"])


def test_no_step_ends_in_a_stray_continuation():
    """A `run:` block whose last line is an argument is a step cut in half.

    Moving the release out of the image job left `--title …` behind, and the
    shell ran it as a command: "--title: command not found", which failed
    the image build for a reason that had nothing to do with the image.
    """
    import re

    for path in WORKFLOWS:
        document = yaml.safe_load(path.read_text())
        for job in document["jobs"].values():
            for step in job.get("steps", []):
                script = step.get("run")
                if not script:
                    continue
                continued = False
                for line in script.splitlines():
                    stripped = line.strip()
                    if not stripped or stripped.startswith("#"):
                        continued = False
                        continue
                    # An argument line is fine when the line before it ended
                    # with a backslash; orphaned, it is a command.
                    if not continued:
                        assert not re.match(r"^--[a-z]", stripped), \
                            f"{path.name}: {step.get('name')}: stray {stripped[:40]!r}"
                    continued = stripped.endswith("\\")


def test_the_release_tells_the_truth_about_the_image():
    """A version whose image build failed must say so, not claim an image.

    The first run of this said "OS image ghcr...:v0.1.0-pre.022, signed"
    for a version whose image had failed, because the note keyed off
    whether the image was MEANT to be built.
    """
    ci = (ROOT / ".github/workflows/ci.yml").read_text()
    assert '[ "${{ needs.image.result }}" = "failure" ] && failed=--image-failed' in ci
    # And whether an image EXISTS is the registry's answer, not the plan's
    # (see test_publish_releases.py).
    notes = (ROOT / "scripts/release-notes.py").read_text()
    assert "The image build failed, so no machine can move to this version" in notes
    assert "No new image" in notes, "and the skipped-on-purpose case still reads well"


def test_the_docs_are_rebuilt_in_the_same_run_as_the_release():
    """A release made by CI cannot trigger another workflow.

    GitHub deliberately does not start workflow runs from events its own
    token created, so a separate docs workflow listening for `release:`
    never fired and the published Releases page sat several versions
    behind with nothing looking broken. The docs build is a job in this
    run, after the release.
    """
    document = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    docs = document["jobs"]["docs"]
    assert docs["needs"] == ["release"]
    assert "always()" in docs["if"], "a skipped release must not skip the docs"
    steps = " ".join(str(step) for step in docs["steps"])
    assert "actions/deploy-pages" in steps and "build-docs.py" in steps
    assert not (ROOT / ".github/workflows/docs.yml").exists(), \
        "two workflows deploying Pages race each other"


def test_nothing_waits_for_an_event_its_own_token_cannot_fire():
    for path in WORKFLOWS:
        document = yaml.safe_load(path.read_text())
        triggers = document.get(True) or document.get("on") or {}
        if isinstance(triggers, dict):
            assert "release" not in triggers, \
                f"{path.name}: a CI-made release does not fire this"


def test_the_tag_is_the_version_and_nothing_is_committed_for_it():
    """No version job, no tick commit, no VERSION file.

    The pre-commit hook numbered every commit on every branch; then a
    version job committed one tick per merge and built that commit — which
    spent the number before the build had passed, and lost four numbers to
    release steps that failed. Now the image job builds the pushed commit
    itself with the number scripts/version.py counts from the tags, and
    the release job pushes the tag last (see test_version.py and
    test_publish_releases.py).
    """
    ci = (ROOT / ".github/workflows/ci.yml").read_text()
    document = yaml.safe_load(ci)
    jobs = document["jobs"]
    assert "version" not in jobs
    assert "[skip ci]" not in ci and "HEAD:master" not in ci, "CI pushes no commits"
    assert jobs["image"]["needs"] == ["unit", "compile", "shell", "changes"]
    assert "SHA: ${{ github.sha }}" in ci, "the image is built from the pushed commit"
    assert "scripts/version.py release" in ci
    release = jobs["release"]
    assert "version" not in release["needs"]
    assert release["concurrency"]["group"] == "release"
    assert release["concurrency"]["cancel-in-progress"] is False
    assert '--sha "$GITHUB_SHA"' in ci
    for job in ("image", "release"):
        checkout = jobs[job]["steps"][0]
        assert checkout["with"]["fetch-depth"] == 0, f"{job}: the version is counted from the tags"
    # And the dev shell no longer installs the hook.
    nix = (ROOT / "devenv.nix").read_text()
    assert "git-hooks.hooks.version-bump" not in nix


def test_the_image_carries_its_version_where_bootc_reads_it():
    # bootc prints "Version:" from org.opencontainers.image.version — for
    # the running deployment and for the update it offers. The admin app's
    # check could only say "an update is available" until the label was ours.
    containerfile = (ROOT / "os-image/Containerfile").read_text()
    assert 'LABEL org.opencontainers.image.version="${KOSHER_VERSION}"' in containerfile
    assert containerfile.index("ARG KOSHER_VERSION") < containerfile.index(
        "LABEL org.opencontainers.image.version")
    assert containerfile.rstrip().endswith('"${KOSHER_VERSION}"'), \
        "last, so a new version number does not rebuild every layer"
    for path in (".github/workflows/ci.yml", "Justfile"):
        text = (ROOT / path).read_text()
        assert "--build-arg" in text and "KOSHER_VERSION=" in text, path
        assert "scripts/version.py" in text, f"{path}: the number comes from the tags"
        assert "VERSION)" not in text and "< VERSION" not in text, \
            f"{path}: there is no VERSION file to read"
    # The same argument writes the number into the image for os-release,
    # the installer and the daemon; an empty one fails the build.
    assert 'test -n "$KOSHER_VERSION"' in containerfile
    assert '"$KOSHER_VERSION" > /usr/share/kosher/VERSION' in containerfile


def test_every_pull_request_that_reaches_the_machine_is_tested_in_a_vm():
    """vm-test.yml runs `just pr-test`, the recipe a developer runs (#173).

    It must stop loudly on a runner without KVM rather than emulate for
    hours, keep to same-repository pull requests like try-image.yml, upload
    what it saw whatever the result, and never ask for the GPU mode."""
    text = (ROOT / ".github/workflows/vm-test.yml").read_text()
    document = yaml.safe_load(text)
    triggers = document.get(True) or document.get("on")
    assert "pull_request" in triggers
    gate = " ".join(document["jobs"]["changes"]["if"].split())
    assert "head.repo.full_name == github.repository" in gate
    build = document["jobs"]["build"]
    assert build["needs"] == ["changes"]
    built = "\n".join(step.get("run", "") for step in build["steps"])
    assert "podman build" in built and "--cache-from" in built, "the layers come from the cache"
    assert "podman push --digestfile" in built, "the runners switch to the image by digest"
    assert "skopeo inspect" in built and ":edge" in built, \
        "a change to nothing in the image tests master's own"
    assert build["outputs"]["image"]
    vm = document["jobs"]["vm"]
    assert vm["needs"] == ["changes", "build"]
    # Built once, tested side by side: the desktop and each suite group on
    # a runner of its own, every part reporting even when another fails.
    assert vm["strategy"]["fail-fast"] is False
    shards = [part["shard"] for part in vm["strategy"]["matrix"]["include"]]
    assert "desktop" in shards
    n = sum(1 for x in shards if x.startswith("suites:"))
    assert {x for x in shards if x != "desktop"} == {f"suites:{k}/{n}" for k in range(1, n + 1)}
    runs = "\n".join(step.get("run", "") for step in vm["steps"])
    assert "just pr-test" in runs
    assert "--registry-image" in runs and "--shard" in runs, "the runners test the pushed image"
    assert "--prefer-base" in runs, "master's base when the cache has one, an install otherwise"
    assert "needs.build.outputs.image" in str(vm["steps"])
    restores = [s for s in vm["steps"] if s.get("uses", "").startswith("actions/cache/restore")]
    assert restores and restores[0]["with"]["restore-keys"] == "pr-test-base-", \
        "the newest base master saved"
    assert "/dev/kvm" not in "".join(s.get("run", "") for s in build["steps"]), \
        "the build job makes no VM"
    assert "/dev/kvm" in runs and "exit 1" in runs, "no KVM is a failure, not a slow run"
    assert "99-kvm4all.rules" in runs, "the runner's /dev/kvm needs the udev rule"
    assert "--gl" not in runs and "KOSHER_GL" not in text, "runners have no GPU"
    uploads = [s for s in vm["steps"] if s.get("uses", "").startswith("actions/upload-artifact")]
    assert uploads and uploads[0]["if"] == "always()"
    assert "!build/pr-test/ci/**/*.qcow2" in uploads[0]["with"]["path"]
    assert "id_ed25519" in uploads[0]["with"]["path"], "the run's private key stays behind"
    # On the job: a run some other label starts (and skips) must not cancel
    # the VM test in progress.
    assert "concurrency" not in document
    assert vm["concurrency"]["cancel-in-progress"] is True
    assert "matrix.name" in vm["concurrency"]["group"], "the parts must not cancel each other"
    assert "matrix.name" in uploads[0]["with"]["name"], "one artifact per part"


def test_master_saves_the_base_the_vm_tests_start_from():
    # ci.yml installs master's image once, after each merge that changes
    # it, and the runners restore it from the cache (vm-test.yml) instead
    # of each installing from scratch. Nothing waits for it.
    ci = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    base = ci["jobs"]["base"]
    assert base["needs"] == ["image"]
    assert "needs.image.result == 'success'" in base["if"]
    runs = "\n".join(step.get("run", "") for step in base["steps"])
    assert "--base-only" in runs and "--registry-image" in runs
    assert "${GITHUB_SHA::12}" in runs, "the image this run pushed, not :edge, which may move"
    saves = [s for s in base["steps"] if s.get("uses", "").startswith("actions/cache/save")]
    assert saves and saves[0]["with"]["key"] == "pr-test-base-${{ github.sha }}"
    assert saves[0]["with"]["path"] == "~/.cache/kosher-pr-test"
    vm = yaml.safe_load((ROOT / ".github/workflows/vm-test.yml").read_text())["jobs"]["vm"]
    restore = [s for s in vm["steps"] if s.get("uses", "").startswith("actions/cache/restore")][0]
    assert restore["with"]["path"] == saves[0]["with"]["path"]
    for job in (ci["jobs"]["release"], ci["jobs"]["docs"]):
        assert "base" not in job["needs"], "nothing waits for the base"


def test_a_pull_request_gets_a_vm_test_only_with_the_label():
    # The merge train tests batches; one pull request's own VM test is on
    # request, and adding another label does not start (or cancel) one.
    document = yaml.safe_load((ROOT / ".github/workflows/vm-test.yml").read_text())
    triggers = document.get(True) or document.get("on")
    assert "labeled" in triggers["pull_request"]["types"]
    assert set(triggers["workflow_call"]["inputs"]) == {"ref", "id"}
    gate = " ".join(document["jobs"]["changes"]["if"].split())
    assert "contains(github.event.pull_request.labels.*.name, 'vm-test')" in gate
    assert "github.event.label.name == 'vm-test'" in gate
    assert "inputs.ref" in document["jobs"]["vm"]["steps"][0]["with"]["ref"]


def _train():
    return yaml.safe_load((ROOT / ".github/workflows/merge-train.yml").read_text())


def test_the_train_tests_a_batch_once_and_lands_it():
    train = _train()
    jobs = train["jobs"]
    # One train at a time, never cancelling the one running.
    assert "merge-train" in train["concurrency"]["group"]
    assert train["concurrency"]["cancel-in-progress"] is False
    # The VM test is the same workflow a pull request would run.
    assert jobs["vm"]["uses"] == "./.github/workflows/vm-test.yml"
    assert jobs["land"]["needs"] == ["assemble", "checks", "vm"]
    land = jobs["land"]["steps"][0]["run"]
    # master takes pull requests only, with a linear history.
    assert "gh pr merge" in land and "--squash" in land
    assert 'git push' not in land.split("refs/heads/train")[0], "no push to master"
    # Its merges start nothing, so it asks for the release.
    assert "gh workflow run ci.yml" in land and "landed=true" in land
    # A failed batch is split; a failure on its own is told and dequeued.
    assert "queued-solo" in land
    # It starts the next train only after moving the queue on.
    assert '"$progress" = 1' in land


def test_the_train_leaves_out_forks_and_what_does_not_merge():
    build = _train()["jobs"]["assemble"]["steps"][1]["run"]
    assert "isCrossRepository | not" in build
    assert "git merge --abort" in build


def test_a_landed_train_is_released_like_a_push():
    ci = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    triggers = ci.get(True) or ci.get("on")
    assert triggers["workflow_dispatch"]["inputs"]["landed"]["type"] == "boolean"
    assert "inputs.landed" in ci["jobs"]["release"]["if"]


def test_the_vm_harness_never_needs_sudo():
    """The recipes `just pr-test` calls, and what they depend on, run no
    sudo; scripts/check-no-sudo.py is the check, and it must also catch a
    recipe that does (just vm, through _sudo and _bib)."""
    import shutil
    import subprocess
    import sys

    if shutil.which("just") is None:
        pytest.skip("just is not installed")
    check = [sys.executable, str(ROOT / "scripts/check-no-sudo.py")]
    clean = subprocess.run(check, cwd=ROOT, capture_output=True, text=True)
    assert clean.returncode == 0, clean.stdout + clean.stderr
    dirty = subprocess.run(check + ["vm"], cwd=ROOT, capture_output=True, text=True)
    assert dirty.returncode == 1 and "_sudo" in dirty.stdout


def test_the_train_lands_only_the_commit_it_tested_and_may_carry_a_workflow_change():
    # A push to a queued pull request after assembly used to land untested
    # (plain `gh pr merge`); and the workflow's own token cannot push a
    # workflow file's new content, so a MERGE_TRAIN_TOKEN secret, when
    # there is one, does the push and the merges instead.
    train = _train()
    assemble = train["jobs"]["assemble"]
    checkout = assemble["steps"][0]
    assert checkout["with"]["token"] == "${{ secrets.MERGE_TRAIN_TOKEN || github.token }}"
    build = assemble["steps"][1]["run"]
    assert 'heads="$heads $n:$(git rev-parse "pr-$n")"' in build
    assert "heads" in assemble["outputs"]
    land = train["jobs"]["land"]["steps"][0]
    assert land["env"]["MERGE_TRAIN_TOKEN"] == "${{ secrets.MERGE_TRAIN_TOKEN }}"
    run = land["run"]
    assert '--match-head-commit "$(head_of "$n")"' in run
    assert 'GH_TOKEN="${MERGE_TRAIN_TOKEN:-$GH_TOKEN}" gh pr merge' in run
    # Labels and comments stay with the workflow's token: a label by the
    # train's own token would wake another train.
    assert 'GH_TOKEN="${MERGE_TRAIN_TOKEN:-$GH_TOKEN}" gh pr edit' not in run
    # A merge by the token is a push and releases itself; only the
    # workflow's own merges need the ask.
    assert '[ "$landed" = 1 ] && [ -z "$MERGE_TRAIN_TOKEN" ]' in run
    assert "gh workflow run ci.yml" in run and "landed=true" in run
