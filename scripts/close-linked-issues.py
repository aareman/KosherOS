#!/usr/bin/env python3
"""Close the open issues whose work has landed.

    scripts/close-linked-issues.py [--dry-run]

An issue is closed when a pull request linked to it has been merged and no
pull request linked to it is still open. "Linked" is GitHub's own link: a
"Closes #N" line in the pull request, or the Development box on either
side. A mention is not a link, so an umbrella issue that many pull
requests mention (the security audit) stays open.

GitHub closes an issue by itself when a pull request that says "Closes #N"
is merged, but not when the merge was made by the workflow token (the
merge train without MERGE_TRAIN_TOKEN), and not for a link made by hand in
the Development box. This catches both, every time it runs; running it
twice closes nothing twice. Needs `gh`, logged in with issues: write.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

QUERY = """
query($owner: String!, $name: String!, $after: String) {
  repository(owner: $owner, name: $name) {
    issues(states: OPEN, first: 100, after: $after) {
      pageInfo { hasNextPage endCursor }
      nodes {
        number
        title
        closedByPullRequestsReferences(first: 50, includeClosedPrs: true) {
          nodes { number state }
        }
      }
    }
  }
}
"""


def landed_by(issue: dict) -> int | None:
    """The merged pull request to name when closing `issue`, or None while
    it should stay open: nothing linked has merged, or something linked is
    still open."""
    prs = issue.get("closedByPullRequestsReferences", {}).get("nodes") or []
    if any(pr["state"] == "OPEN" for pr in prs):
        return None
    merged = [pr["number"] for pr in prs if pr["state"] == "MERGED"]
    return max(merged) if merged else None


def open_issues(repo: str) -> list[dict]:
    owner, name = repo.split("/", 1)
    issues, after = [], None
    while True:
        argv = ["gh", "api", "graphql", "-f", f"query={QUERY}",
                "-f", f"owner={owner}", "-f", f"name={name}"]
        if after:
            argv += ["-f", f"after={after}"]
        page = json.loads(subprocess.run(argv, check=True, capture_output=True,
                                         text=True).stdout)
        found = page["data"]["repository"]["issues"]
        issues += found["nodes"]
        if not found["pageInfo"]["hasNextPage"]:
            return issues
        after = found["pageInfo"]["endCursor"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", "aareman/KosherOS"))
    parser.add_argument("--dry-run", action="store_true", help="say what would close")
    args = parser.parse_args()

    closed = 0
    for issue in open_issues(args.repo):
        pr = landed_by(issue)
        if pr is None:
            continue
        print(f"#{issue['number']} {issue['title']}: #{pr} was merged and nothing linked is open")
        if args.dry_run:
            continue
        subprocess.run(["gh", "issue", "close", str(issue["number"]), "--repo", args.repo,
                        "--reason", "completed",
                        "--comment", f"Closed because #{pr} was merged and no open pull "
                                     "request is linked to this issue."],
                       check=True)
        closed += 1
    print(f"{closed} closed" if not args.dry_run else "dry run: nothing closed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
