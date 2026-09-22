#!/usr/bin/env python3
"""Every version on master gets a tag and a release — this run's, and any that
an earlier run left behind.

The version job commits the tick to master before anything downstream has
run, and that is by design: the image has to be built from the ticked commit
so the version inside it matches the tag on it. The cost is that a version
is spent the moment it is committed. When the release step then fails, the
number is gone and nothing comes back for it. That happened to 0.3.0: every
job passed, the image was built, pushed and signed, and `gh release create`
answered `HTTP 403: Resource not accessible by integration` — once, for no
reason that survived into the next run. The next merge became 0.4.0, and the
releases page read 0.2.0, 0.4.0.

Versions are meant to be sequential, so the release step now does two things
the old inline shell did not:

  * it retries the API call, because one 403 is not a decision;
  * it publishes EVERY version tick on this history that has no tag yet, not
    only the newest one, so a version that slipped through one run is caught
    by the next and the sequence heals itself.

A release made after the fact is created with `--latest=false`: the newest
version keeps the "Latest" badge, and the stable-promotion workflow reads
that badge. Whether such a release can say "here is the image" is answered
by the registry, not by this run's outputs, which only know about this run.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NOTES = ROOT / "scripts/release-notes.py"

# The subject the version job writes. Plain semver only: the old
# 0.1.0-pre.N ticks were all released in their day and are not backfilled.
TICK = re.compile(r"^chore: version (\d+\.\d+\.\d+)\b")

ATTEMPTS = 3
BACKOFF = 15  # seconds; doubled each time

MANIFEST_TYPES = ", ".join([
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.docker.distribution.manifest.v2+json",
])


def git(*args: str, cwd: Path = ROOT) -> str:
    return subprocess.run(["git", *args], check=True, capture_output=True,
                          text=True, cwd=cwd).stdout.strip()


def existing_tags(cwd: Path = ROOT) -> set[str]:
    return set(git("tag", "--list", "v*", cwd=cwd).split())


def pending_versions(sha: str, cwd: Path = ROOT) -> list[tuple[str, str]]:
    """(commit, version) for every version tick on `sha`'s history with no
    tag, oldest first.

    Only ticks on THIS history: re-running an old workflow must not release
    versions that came after it. Only ticks whose VERSION file says what the
    subject says, so a hand-edited VERSION is never released by accident —
    the merge that follows it is.
    """
    log = git("log", "--reverse", "--format=%H%x09%s", sha, "--", "VERSION", cwd=cwd)
    tags = existing_tags(cwd)
    pending = []
    for line in log.splitlines():
        commit, _, subject = line.partition("\t")
        found = TICK.match(subject)
        if not found:
            continue
        version = found.group(1)
        if f"v{version}" in tags:
            continue
        if git("show", f"{commit}:VERSION", cwd=cwd).strip() != version:
            continue
        pending.append((commit, version))
    return pending


def previous_tag(commit: str, cwd: Path = ROOT) -> str:
    """The last version tag before this commit, by history rather than by
    date: a tag made after the fact carries the wrong date."""
    try:
        return git("describe", "--tags", "--abbrev=0", "--match", "v*", f"{commit}^", cwd=cwd)
    except subprocess.CalledProcessError:
        return ""


def gh(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["gh", *args], capture_output=True, text=True)


def release_exists(tag: str) -> bool:
    return gh("release", "view", tag).returncode == 0


def image_exists(image: str, tag: str) -> bool:
    """Whether the registry holds `image:tag` — asked of the registry itself,
    because a run only knows whether IT built an image, and a backfilled
    version was built by some earlier run."""
    registry, _, repository = image.partition("/")
    if not repository:
        return False
    try:
        token_request = urllib.request.Request(
            f"https://{registry}/token?scope=repository:{repository}:pull")
        # Anonymous is enough for a public image; a token lets a private one
        # answer too. ghcr takes the same user:token pair `podman login` does.
        token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
        if token:
            pair = f"{os.environ.get('GITHUB_ACTOR', 'github-actions')}:{token}".encode()
            token_request.add_header("Authorization", "Basic " + base64.b64encode(pair).decode())
        with urllib.request.urlopen(token_request, timeout=30) as response:
            bearer = json.load(response)["token"]
        manifest = urllib.request.Request(
            f"https://{registry}/v2/{repository}/manifests/{tag}", method="HEAD",
            headers={"Authorization": f"Bearer {bearer}", "Accept": MANIFEST_TYPES})
        with urllib.request.urlopen(manifest, timeout=30) as response:
            return response.status == 200
    except (urllib.error.URLError, OSError, KeyError, ValueError):
        return False


def notes_for(version: str, previous: str, commit: str, image: str, built: str,
              image_failed: bool) -> str:
    """The notes are written by scripts/release-notes.py, which is read and
    tested like any other code."""
    command = [sys.executable, str(NOTES), "--version", version, "--previous", previous,
               "--sha", commit, "--image", image, "--built", built]
    if image_failed:
        command.append("--image-failed")
    return subprocess.run(command, check=True, capture_output=True, text=True).stdout


def create_release(tag: str, commit: str, version: str, notes: Path, *, latest: bool,
                   sleep=time.sleep) -> None:
    """`gh release create`, tried more than once: the failure that made the
    0.3.0 gap was a 403 that the very next run did not see."""
    command = ["release", "create", tag, "--target", commit, "--title", f"KosherOS {version}",
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


def publish(sha: str, image: str, built: str, image_failed: bool = False, *,
            cwd: Path = ROOT, workdir: Path | None = None, sleep=time.sleep) -> list[str]:
    """Create the tag and the release for every unreleased version on `sha`'s
    history, oldest first. Returns the tags made."""
    head = git("rev-parse", sha, cwd=cwd)
    pending = pending_versions(head, cwd)
    if not pending:
        print("every version on this history already has a release")
        return []
    workdir = workdir or cwd
    made = []
    for commit, version in pending:
        tag = f"v{version}"
        if release_exists(tag):
            # The release is there and the tag is not fetched yet: nothing
            # to do but let the next version see it as its previous.
            print(f"{tag}: release already exists; leaving it")
            subprocess.run(["git", "tag", tag, commit], cwd=cwd, capture_output=True)
            continue
        this_run = commit == head
        if this_run:
            has_image, failed = built, image_failed
        else:
            has_image = "yes" if image_exists(image, tag) else "no"
            failed = False
            print(f"{tag}: left behind by an earlier run"
                  f" ({'its image is in the registry' if has_image == 'yes' else 'no image for it'})")
        previous = previous_tag(commit, cwd)
        notes = workdir / f"release-notes-{tag}.md"
        notes.write_text(notes_for(version, previous, commit, image, has_image, failed))
        print(f"--- {tag} (since {previous or 'the beginning'}) ---")
        print(notes.read_text())
        create_release(tag, commit, version, notes, latest=this_run, sleep=sleep)
        # Locally too, so the next version's "previous" is this one.
        subprocess.run(["git", "tag", tag, commit], cwd=cwd, capture_output=True)
        made.append(tag)
    return made


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--sha", default="HEAD", help="the commit this run built")
    parser.add_argument("--image", default="ghcr.io/aareman/kosher-linux")
    parser.add_argument("--built", choices=["yes", "no"], default="no",
                        help="whether THIS run pushed an image for --sha")
    parser.add_argument("--image-failed", action="store_true",
                        help="this run's image build failed")
    parser.add_argument("--dry-run", action="store_true",
                        help="say what would be released and stop")
    args = parser.parse_args(argv)
    if args.dry_run:
        head = git("rev-parse", args.sha)
        pending = pending_versions(head)
        if not pending:
            print("every version on this history already has a release")
        for commit, version in pending:
            print(f"v{version}  {commit[:12]}  since {previous_tag(commit) or 'the beginning'}"
                  f"{'  (this run)' if commit == head else ''}")
        return 0
    publish(args.sha, args.image, args.built, args.image_failed)
    return 0


if __name__ == "__main__":
    sys.exit(main())
