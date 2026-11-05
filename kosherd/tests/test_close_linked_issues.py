"""Closing an issue once its work has landed (scripts/close-linked-issues.py)."""

import importlib.util
from pathlib import Path

ROOT = Path(__file__).parents[2]
spec = importlib.util.spec_from_file_location(
    "close_linked_issues", ROOT / "scripts/close-linked-issues.py")
close = importlib.util.module_from_spec(spec)
spec.loader.exec_module(close)


def issue(*prs):
    return {"number": 1, "title": "t",
            "closedByPullRequestsReferences": {"nodes": [
                {"number": n, "state": s} for n, s in prs]}}


def test_a_merged_link_and_nothing_open_closes_it():
    assert close.landed_by(issue((290, "MERGED"))) == 290


def test_it_names_the_latest_merged_pull_request():
    assert close.landed_by(issue((275, "MERGED"), (280, "MERGED"), (277, "CLOSED"))) == 280


def test_a_linked_pull_request_still_open_keeps_it_open():
    # #271: one fix merged, the other still in review.
    assert close.landed_by(issue((275, "MERGED"), (277, "OPEN"))) is None


def test_nothing_merged_keeps_it_open():
    assert close.landed_by(issue()) is None
    assert close.landed_by(issue((12, "CLOSED"))) is None, "closed unmerged is not done"


def test_the_workflow_runs_the_default_branchs_script_only_for_merges():
    text = (ROOT / ".github/workflows/close-linked-issues.yml").read_text()
    assert "pull_request_target" in text
    assert "github.event.pull_request.merged" in text
    # Never the pull request's own code with a token that closes issues.
    assert "ref: ${{ github.event.repository.default_branch }}" in text
    assert "python3 scripts/close-linked-issues.py" in text
