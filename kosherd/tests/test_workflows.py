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
