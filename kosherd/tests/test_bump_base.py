"""The base images come from our own copy of Fedora's, pinned by digest.

`FROM fedora-bootc:44` moved about once a day and threw away the build
cache; pinning Fedora's own digest broke every build a day later, because
quay.io deletes a digest once its tag moves (#242). The base is copied
into our registry under a dated tag that stays, and the FROMs name it.
"""

import importlib.util
import re
from pathlib import Path

ROOT = Path(__file__).parents[2]
SPEC = importlib.util.spec_from_file_location("bump_base", ROOT / "scripts/bump-base.py")
bump = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bump)

D = "sha256:" + "b" * 64
OLD = "sha256:" + "a" * 64


def test_every_from_names_our_dated_copy_by_digest():
    text = (ROOT / "os-image/Containerfile").read_text()
    froms = re.findall(r"^FROM\s+(\S+)", text, re.M)
    assert froms, "no FROM found"
    for ref in froms:
        assert re.fullmatch(r"ghcr\.io/aareman/kosher-base-fedora(-bootc)?:44-\d{8}@sha256:[0-9a-f]{64}",
                            ref), ref


def test_fedoras_tag_an_old_pin_or_an_old_copy_all_become_the_new_copy():
    text = ("FROM quay.io/fedora/fedora:44 as keyd-build\n"
            f"FROM quay.io/fedora/fedora-bootc:44@{OLD}\n"
            f"FROM ghcr.io/aareman/kosher-base-fedora:44-20261001@{OLD} as glass-build\n"
            "RUN echo FROM quay.io/fedora/fedora:44 stays in a RUN\n")
    refs = {"fedora": f"ghcr.io/aareman/kosher-base-fedora:44-20261029@{D}",
            "fedora-bootc": f"ghcr.io/aareman/kosher-base-fedora-bootc:44-20261029@{D}"}
    assert bump.pinned(text, refs).splitlines() == [
        f"FROM ghcr.io/aareman/kosher-base-fedora:44-20261029@{D} as keyd-build",
        f"FROM ghcr.io/aareman/kosher-base-fedora-bootc:44-20261029@{D}",
        f"FROM ghcr.io/aareman/kosher-base-fedora:44-20261029@{D} as glass-build",
        "RUN echo FROM quay.io/fedora/fedora:44 stays in a RUN",
    ]


def test_the_copy_keeps_fedoras_digest_and_every_architecture(tmp_path, monkeypatch):
    calls = []

    def run(argv, **_kw):
        calls.append(argv)
        out = D + "\n" if "inspect" in argv else ""
        return type("R", (), {"returncode": 0, "stdout": out, "stderr": ""})

    containerfile = tmp_path / "Containerfile"
    containerfile.write_text("FROM quay.io/fedora/fedora-bootc:44\n")
    monkeypatch.setattr(bump, "CONTAINERFILE", containerfile)
    monkeypatch.setattr(bump, "ROOT", tmp_path)
    assert bump.main([], run=run, today="20261029") == 0
    copy = next(c for c in calls if "copy" in c)
    assert "--all" in copy and "--preserve-digests" in copy
    assert copy[-2:] == ["docker://quay.io/fedora/fedora-bootc:44",
                         "docker://ghcr.io/aareman/kosher-base-fedora-bootc:44-20261029"]
    assert containerfile.read_text() == \
        f"FROM ghcr.io/aareman/kosher-base-fedora-bootc:44-20261029@{D}\n"


def test_a_weekly_bump_goes_through_the_train():
    import yaml

    wf = yaml.safe_load((ROOT / ".github/workflows/bump-base.yml").read_text())
    triggers = wf.get(True) or wf.get("on")
    assert "schedule" in triggers
    assert wf["permissions"]["packages"] == "write"
    steps = "\n".join(s.get("run", "") for s in wf["jobs"]["bump"]["steps"])
    assert "skopeo login ghcr.io" in steps and "scripts/bump-base.py" in steps
    assert "--label queued" in steps and "gh workflow run merge-train.yml" in steps
