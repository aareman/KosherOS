#!/usr/bin/env python3
"""Tag and release the commit CI just built, and finish any release an
earlier run left half-done.

The order is the point. A version is spent the moment its tag exists, so the
tag is pushed LAST — after the tests, after the image is built, pushed and
signed — and everything that follows it can be finished by a later run:

    1. work out the version this commit would be (scripts/version.py: the
       last tag reachable from it plus the step its commits ask for);
    2. push the tag, with git, to this commit. A tag push is atomic: if the
       remote already has that tag the push is rejected and nothing has
       happened, so two runs can never spend one number twice;
    3. give the image the version's tag in the registry (a server-side copy
       of the sha-tagged image CI already pushed) and sign it;
    4. create the GitHub release on the tag that now exists.

If anything fails before step 2, no number is spent: the next merge works
out the same number and its notes list both merges' changes. If anything
fails after it, the tag exists without a release, and every run also walks
the version tags that have no release and finishes them — which is how the
four tags a person pushed by hand after the last design failed get their
releases too.

Why the tag is pushed with git rather than made by the releases API: since
November 2023 GitHub demands the `workflows` scope to create a release with
an explicit target commit when that commit has no ref of its own and a
workflow file changed between it and the branch tip — and the Actions token
can never hold that scope. That is the `HTTP 403: Resource not accessible
by integration` that killed 0.3.0 and then, through a backfill that
retargeted the same commit, 0.4.2, 0.5.0 and 0.6.0. A release created on an
existing tag, with no target, does not go near that rule.

A release made for an older tag is created with `--latest=false`: the newest
version keeps the "Latest" badge, which the stable promotion reads.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NOTES = ROOT / "scripts/release-notes.py"

_spec = importlib.util.spec_from_file_location("kosher_version", ROOT / "scripts/version.py")
version_tool = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(version_tool)

# Plain semver tags only: the old v0.1.0-pre.N tags all have their releases
# and are not looked at again.
VERSION_TAG = re.compile(r"^v(\d+\.\d+\.\d+)$")

ATTEMPTS = 3
BACKOFF = 15  # seconds; doubled each time
RESERVE_ATTEMPTS = 5


def git(*args: str, cwd: Path = ROOT, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], check=check, capture_output=True, text=True, cwd=cwd)


def out(*args: str, cwd: Path = ROOT) -> str:
    return git(*args, cwd=cwd).stdout.strip()


def gh(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["gh", *args], capture_output=True, text=True)


def sh(*args: str) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(list(args), capture_output=True, text=True)
    except FileNotFoundError as missing:
        return subprocess.CompletedProcess(list(args), 127, "", f"{missing.filename}: not installed")


# --- what exists ---------------------------------------------------------------

def version_tags(cwd: Path = ROOT) -> dict[str, str]:
    """tag -> commit for every plain version tag, after a fetch."""
    git("fetch", "--tags", "--quiet", "origin", cwd=cwd, check=False)
    tags = {}
    for line in out("tag", "--list", "v[0-9]*", cwd=cwd).splitlines():
        if VERSION_TAG.match(line):
            tags[line] = out("rev-list", "-n1", line, cwd=cwd)
    return tags


def released_tags() -> set[str]:
    result = gh("release", "list", "--limit", "500", "--json", "tagName")
    if result.returncode != 0:
        raise SystemExit(f"could not list the releases: {result.stderr.strip()}")
    return {entry["tagName"] for entry in json.loads(result.stdout or "[]")}


def release_exists(tag: str) -> bool:
    return gh("release", "view", tag).returncode == 0


def image_exists(image: str, tag: str) -> bool:
    """Whether the registry holds `image:tag` — asked of the registry itself,
    because a run only knows what IT pushed."""
    return sh("skopeo", "inspect", "--raw", f"docker://{image}:{tag}").returncode == 0


def tag_image(image: str, source: str, tag: str) -> None:
    """Give the image CI pushed under its sha the version's tag, and sign
    it. A server-side copy: nothing is pulled."""
    copied = sh("skopeo", "copy", "--all", f"docker://{image}:{source}", f"docker://{image}:{tag}")
    if copied.returncode != 0:
        raise SystemExit(f"{tag}: could not tag the image: {copied.stderr.strip()}")
    signed = sh("cosign", "sign", "--yes", f"{image}:{tag}")
    if signed.returncode != 0:
        raise SystemExit(f"{tag}: could not sign the image: {signed.stderr.strip()}")


# --- the steps -----------------------------------------------------------------

def reserve(commit: str, cwd: Path = ROOT) -> str | None:
    """Push the version tag for this commit. Returns the tag, or None when
    this commit's changes were already released inside a later version.

    The push is what makes the number ours: a tag the remote already has is
    rejected outright. Then either the remote's tag is on this very commit
    (a re-run: nothing to do), or it is on a commit that CONTAINS this one
    (a merge right after ours took the number first, and our changes are in
    its release: nothing to do), or it is on a commit this one contains
    (we came second: the number is taken, count again from it).
    """
    for _ in range(RESERVE_ATTEMPTS):
        version = version_tool.release_version(commit, cwd)
        tag = f"v{version}"
        pushed = git("push", "--quiet", "origin", f"{commit}:refs/tags/{tag}", cwd=cwd, check=False)
        if pushed.returncode == 0:
            git("tag", "-f", tag, commit, cwd=cwd, check=False)
            print(f"{tag}: tagged {commit[:12]}")
            return tag
        git("fetch", "--tags", "--quiet", "origin", cwd=cwd, check=False)
        theirs = out("rev-list", "-n1", tag, cwd=cwd) if git(
            "rev-parse", "--verify", "--quiet", f"refs/tags/{tag}", cwd=cwd, check=False
        ).returncode == 0 else None
        if theirs is None:
            print(f"{tag}: the push failed: {pushed.stderr.strip()}", file=sys.stderr)
            raise SystemExit(f"{tag}: could not push the tag")
        if theirs == commit:
            print(f"{tag}: already on {commit[:12]}")
            return tag
        if git("merge-base", "--is-ancestor", commit, theirs, cwd=cwd, check=False).returncode == 0:
            print(f"{tag}: already released from {theirs[:12]}, which contains {commit[:12]}; "
                  "this merge's changes are in it")
            return None
        if git("merge-base", "--is-ancestor", theirs, commit, cwd=cwd, check=False).returncode == 0:
            print(f"{tag}: taken by {theirs[:12]} meanwhile; counting again from it")
            continue
        raise SystemExit(f"{tag} is on {theirs[:12]}, which is unrelated to {commit[:12]}")
    raise SystemExit("could not settle on a version after several tries")


def previous_tag(commit: str, cwd: Path = ROOT) -> str:
    """The last version tag before this commit, by history rather than by
    date: a tag made after the fact carries the wrong date."""
    return version_tool.last_tag(f"{commit}^", cwd) or ""


def notes_for(version: str, previous: str, commit: str, image: str, built: str,
              image_failed: bool) -> str:
    """The notes are written by scripts/release-notes.py, which is read and
    tested like any other code."""
    command = [sys.executable, str(NOTES), "--version", version, "--previous", previous,
               "--sha", commit, "--image", image, "--built", built]
    if image_failed:
        command.append("--image-failed")
    return subprocess.run(command, check=True, capture_output=True, text=True).stdout


def create_release(tag: str, version: str, notes: Path, *, latest: bool,
                   sleep=time.sleep) -> None:
    """`gh release create` on a tag that exists, tried more than once. No
    target: the tag says where the release is."""
    command = ["release", "create", tag, "--title", f"KosherOS {version}",
               "--notes-file", str(notes)]
    if not latest:
        command.append("--latest=false")
    delay = BACKOFF
    for attempt in range(1, ATTEMPTS + 1):
        result = gh(*command)
        if result.returncode == 0:
            return
        if release_exists(tag):
            print(f"{tag}: another run made this release first; leaving it")
            return
        print(f"{tag}: attempt {attempt} of {ATTEMPTS} failed: "
              f"{(result.stderr or result.stdout).strip()}", file=sys.stderr)
        if attempt < ATTEMPTS:
            sleep(delay)
            delay *= 2
    raise SystemExit(f"{tag}: could not create the release after {ATTEMPTS} attempts")


def finish(tag: str, commit: str, image: str, *, latest: bool, image_failed: bool = False,
           cwd: Path = ROOT, workdir: Path | None = None, sleep=time.sleep) -> bool:
    """Everything after the tag: the image's version tag, then the release.
    Safe to run again. Returns whether a release was created."""
    version = tag[1:]
    if not image_exists(image, tag) and image_exists(image, commit[:12]):
        tag_image(image, commit[:12], tag)
        print(f"{tag}: the image is tagged and signed")
    built = "yes" if image_exists(image, tag) else "no"
    if release_exists(tag):
        print(f"{tag}: release exists")
        return False
    previous = previous_tag(commit, cwd)
    notes = (workdir or cwd) / f"release-notes-{tag}.md"
    notes.write_text(notes_for(version, previous, commit, image, built, image_failed))
    print(f"--- {tag} (since {previous or 'the beginning'}; image: {built}) ---")
    print(notes.read_text())
    create_release(tag, version, notes, latest=latest, sleep=sleep)
    return True


def publish(sha: str, image: str, image_failed: bool = False, *, cwd: Path = ROOT,
            workdir: Path | None = None, sleep=time.sleep) -> list[str]:
    """Tag and release `sha`, then finish every version tag without a
    release. Returns the tags released, this run's first."""
    head = out("rev-parse", sha, cwd=cwd)
    made = []
    tag = reserve(head, cwd)
    if tag and finish(tag, head, image, latest=True, image_failed=image_failed,
                      cwd=cwd, workdir=workdir, sleep=sleep):
        made.append(tag)
    tags = version_tags(cwd)
    released = released_tags()
    # Only tags this commit contains: an older run re-run must not finish a
    # newer version's release, and a merge whose number went to the merge
    # right after it leaves that one to its own run.
    waiting = sorted(
        (t for t in tags if t not in released and t != tag
         and git("merge-base", "--is-ancestor", tags[t], head, cwd=cwd, check=False).returncode == 0),
        key=lambda t: tuple(int(n) for n in t[1:].split(".")))
    for other in waiting:
        print(f"{other}: tagged but never released")
        if finish(other, tags[other], image, latest=False, cwd=cwd, workdir=workdir, sleep=sleep):
            made.append(other)
    if not made:
        print("nothing to release")
    return made


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--sha", default="HEAD", help="the commit this run built")
    parser.add_argument("--image", default="ghcr.io/aareman/kosher-linux")
    parser.add_argument("--image-failed", action="store_true",
                        help="this run's image build failed")
    parser.add_argument("--dry-run", action="store_true",
                        help="say what would be tagged and released and stop")
    args = parser.parse_args(argv)
    if args.dry_run:
        head = out("rev-parse", args.sha)
        tags = version_tags()
        current = version_tool.exact_tag(head)
        print(f"{head[:12]}: {'already ' + current if current else 'would be v' + version_tool.release_version(head)}")
        for tag in sorted(set(tags) - released_tags(), key=lambda t: tuple(int(n) for n in t[1:].split("."))):
            print(f"{tag}: tagged {tags[tag][:12]}, no release"
                  f"{' (image in the registry)' if image_exists(args.image, tag) else ''}")
        return 0
    publish(args.sha, args.image, args.image_failed)
    return 0


if __name__ == "__main__":
    sys.exit(main())
