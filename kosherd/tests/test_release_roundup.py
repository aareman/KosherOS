"""The weekly release, written like a project with something to say.

The per-merge notes were one line from a commit subject ("🔁 System — 1Password
from nixpkgs: ..."). Releases are weekly now, and read like Omarchy's: a
first paragraph naming what the week brings, how to get it, what is
different, each new thing under its own heading with a paragraph, the fixes
one line each. The words come from the pull requests' "Release note"
sections; a pull request without one falls back to its commits' subjects.
"""

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).parents[2]


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


rn = _load("release_notes_roundup", "scripts/release-notes.py")

NOTE_1P = """Closes #234

## Release note
**New** — 1Password's SSH agent and its op command work with the app.
Install 1Password and 1Password CLI from the Store, then turn on the
integration under Settings → Developer.
**What's different:** 1Password comes from the Store's Nix shelf; the Flatpak cannot use the SSH agent.

## What changes
technical details nobody reads in the release
"""

NOTE_FIX = """## Release note
**Fix**: The Store no longer jumps in width while an app installs.
"""

TEMPLATE_UNTOUCHED = (ROOT / ".github/pull_request_template.md").read_text()


def test_a_release_note_is_read_from_its_section():
    note = rn.release_note(NOTE_1P)
    assert note["kind"] == "feat"
    assert note["headline"] == "1Password's SSH agent and its op command work with the app"
    assert note["text"].startswith("Install 1Password")
    assert "technical details" not in note["text"], "the next section is not the note"
    assert note["different"].startswith("1Password comes from")
    assert rn.release_note(NOTE_FIX)["kind"] == "fix"


def test_the_templates_example_is_never_taken_for_a_note():
    # Its guidance and example sit in an HTML comment.
    assert rn.release_note(TEMPLATE_UNTOUCHED) is None
    assert rn.release_note("no section here") is None


def _fake(monkeypatch, commits, prs):
    """commits: (sha, subject, files); prs: number -> (title, body);
    each commit's pull request is the number at its subject's end."""
    monkeypatch.setattr(rn, "range_commits", lambda prev, sha: commits)

    def find(sha):
        subject = next(s for c, s, _f in commits if c == sha)
        m = rn.PR_NUMBER.search(subject)
        return int(m.group(1)) if m else None

    def fetch(number):
        title, body = prs[number]
        return {"title": title, "body": body,
                "url": f"https://github.com/aareman/KosherOS/pull/{number}"}

    return fetch, find


WEEK = [
    ("a1", "1Password from nixpkgs (#235)", ["kosherd/src/kosherd/onepassword.py"]),
    ("a2", "Calmer sites (#232)", ["mitm/kosher_calm.js"]),
    ("a3", "Store: keep the width (#236)", ["store-app/src/kosherstore/app.py"]),
    ("a4", "A merge train (#241)", [".github/workflows/merge-train.yml"]),
]
PRS = {
    235: ("1Password from nixpkgs", NOTE_1P),
    232: ("Calmer versions of the addictive sites", "## Release note\n**New** — Feeds on x.com and reddit end.\n"
                                                    "Turn it on under Extras.\n"),
    236: ("Store: keep the width", NOTE_FIX),
    241: ("A merge train", "no note: CI"),
}


def test_a_week_reads_like_a_release(monkeypatch):
    fetch, find = _fake(monkeypatch, WEEK, PRS)
    text = rn.roundup_notes("0.24.0", "v0.23.20", "abc", "ghcr.io/aareman/kosher-linux", "yes",
                            fetch=fetch, find=find)
    first = text.splitlines()[0]
    assert first.startswith("Two new things and one fix this week:")
    assert "1Password's SSH agent" in first and "feeds on x.com" in first
    assert "bootc switch ghcr.io/aareman/kosher-linux:v0.24.0" in text
    # What is different comes before the new things, and only when there is some.
    assert text.index("## What's different") < text.index("## New")
    # Each new thing is a heading with its paragraph and its pull request.
    assert "### 1Password's SSH agent and its op command work with the app" in text
    assert "Install 1Password and 1Password CLI" in text
    assert "([#232](https://github.com/aareman/KosherOS/pull/232))" in text
    # Fixes are one line each.
    assert "## Fixes\n\n- The Store no longer jumps in width while an app installs." in text
    # CI work is counted, never listed.
    assert "merge train" not in text.lower().split("behind the scenes")[0]
    assert "Behind the scenes: one change to CI." in text
    assert "**Full changelog**: https://github.com/aareman/KosherOS/compare/v0.23.20...v0.24.0" in text


def test_no_heading_without_something_under_it(monkeypatch):
    fetch, find = _fake(monkeypatch, WEEK[2:3], PRS)
    text = rn.roundup_notes("0.24.1", "v0.24.0", "abc", "img", "yes", fetch=fetch, find=find)
    assert "## What's different" not in text and "## New" not in text
    assert text.splitlines()[0] == "One fix this week."


def test_a_pull_request_without_a_note_falls_back_to_its_commits(monkeypatch):
    # A batch of small fixes is several things, not one; a "(#N)" that is
    # an issue's number is resolved to the pull request through GitHub.
    commits = [("b1", "A tried build says when it is ready (#237)", ["kosherd/src/kosherd/daemon.py"]),
               ("b2", "keyd: the same per-app keys are not sent again (#239)", ["kosherd/src/kosherd/keyboard.py"])]
    monkeypatch.setattr(rn, "range_commits", lambda prev, sha: commits)
    found, quiet = rn.entries("v1", "v2", fetch=lambda n: {"title": "Small fixes", "body": "",
                                                           "url": f"u/{n}"},
                              find=lambda sha: 238)
    assert [e["headline"] for e in found] == [
        "A tried build says when it is ready", "The same per-app keys are not sent again"]
    assert {e["number"] for e in found} == {238}
    assert quiet == []


def test_the_weekly_workflow_and_the_per_merge_tag():
    import yaml

    wf = yaml.safe_load((ROOT / ".github/workflows/roundup.yml").read_text())
    triggers = wf.get(True) or wf.get("on")
    assert "schedule" in triggers and "workflow_dispatch" in triggers
    steps = "\n".join(s.get("run", "") for s in wf["jobs"]["roundup"]["steps"])
    assert "publish-releases.py --roundup" in steps
    ci = (ROOT / ".github/workflows/ci.yml").read_text()
    assert "--no-release" in ci, "a merge is tagged and signed, released weekly"


def test_the_roundup_releases_the_newest_version_since_the_last_release(monkeypatch, tmp_path):
    pr = _load("publish_releases_roundup", "scripts/publish-releases.py")
    monkeypatch.setattr(pr, "version_tags", lambda cwd=None: {"v0.23.20": "c20", "v0.23.25": "c25",
                                                              "v0.23.26": "c26"})
    monkeypatch.setattr(pr, "released_tags", lambda: {"v0.23.20"})
    monkeypatch.setattr(pr, "image_exists", lambda image, tag: True)
    made = []
    monkeypatch.setattr(pr, "create_release",
                        lambda tag, version, notes, latest, sleep=None: made.append((tag, latest,
                                                                                     notes.read_text())))
    seen = []
    tag = pr.roundup("img", workdir=tmp_path,
                     notes=lambda newest, previous, sha, built: seen.append((newest, previous, sha, built))
                     or "notes\n")
    assert tag == "v0.23.26"
    assert seen == [("v0.23.26", "v0.23.20", "c26", "yes")], "since the last release, not the last tag"
    assert made == [("v0.23.26", True, "notes\n")]
    # Released already: nothing to do.
    monkeypatch.setattr(pr, "released_tags", lambda: {"v0.23.26"})
    assert pr.roundup("img", workdir=tmp_path, notes=lambda *a: "x") is None
