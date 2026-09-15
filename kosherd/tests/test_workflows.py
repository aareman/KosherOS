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
        assert "runs-on" in job, f"{path.name}: {name} has no runner"


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


def test_the_release_says_when_there_is_no_new_image():
    # The wording lives in scripts/release-notes.py now; the workflow only
    # tells it which case this is (see test_release_notes.py).
    ci = (ROOT / ".github/workflows/ci.yml").read_text()
    assert "scripts/release-notes.py" in ci
    assert "--built" in ci and "--image-failed" in ci


def test_the_image_job_still_tags_and_signs_the_version():
    ci = (ROOT / ".github/workflows/ci.yml").read_text()
    assert 'podman push "$IMAGE:v$version"' in ci
    assert 'cosign sign --yes "$IMAGE:v$version"' in ci


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
    assert "needs.image.result == 'success'" in ci, "the note keys off the result"
    assert '[ "${{ needs.image.result }}" = "failure" ] && failed=--image-failed' in ci
    notes = (ROOT / "scripts/release-notes.py").read_text()
    assert "No image for this version" in notes
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
