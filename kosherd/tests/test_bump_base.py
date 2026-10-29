"""The base images are pinned by digest, and moved on purpose.

`FROM quay.io/fedora/fedora-bootc:44` moved about once a day, and every move
threw away the build cache: twenty-two minutes to change a line of Python.
scripts/bump-base.py pins each FROM to a digest and moves the pins.
"""

import importlib.util
import re
from pathlib import Path

ROOT = Path(__file__).parents[2]
SPEC = importlib.util.spec_from_file_location("bump_base", ROOT / "scripts/bump-base.py")
bump = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bump)

OLD = "sha256:" + "a" * 64
NEW = "sha256:" + "b" * 64


def test_every_from_in_the_containerfile_is_pinned():
    text = (ROOT / "os-image/Containerfile").read_text()
    froms = re.findall(r"^FROM\s+(\S+)", text, re.M)
    assert froms, "no FROM found"
    for ref in froms:
        assert re.fullmatch(r"quay\.io/fedora/fedora(-bootc)?:44@sha256:[0-9a-f]{64}", ref), ref


def test_a_tag_or_an_old_pin_becomes_the_new_pin():
    text = ("FROM quay.io/fedora/fedora:44 as keyd-build\n"
            f"FROM quay.io/fedora/fedora-bootc:44@{OLD}\n"
            "RUN echo FROM quay.io/fedora/fedora:44 stays in a RUN\n")
    out = bump.pinned(text, {"quay.io/fedora/fedora:44": NEW,
                             "quay.io/fedora/fedora-bootc:44": NEW})
    assert out.splitlines() == [
        f"FROM quay.io/fedora/fedora:44@{NEW} as keyd-build",
        f"FROM quay.io/fedora/fedora-bootc:44@{NEW}",
        "RUN echo FROM quay.io/fedora/fedora:44 stays in a RUN",
    ]


def test_the_digest_is_the_multi_arch_index_from_skopeo():
    seen = []

    def run(argv, **_kw):
        seen.append(argv)
        return type("R", (), {"returncode": 0, "stdout": NEW + "\n", "stderr": ""})

    assert bump.digest("quay.io/fedora/fedora:44", run=run) == NEW
    assert seen[0][-1] == "docker://quay.io/fedora/fedora:44"
    assert "inspect" in seen[0]


def test_a_weekly_bump_goes_through_the_train():
    import yaml

    wf = yaml.safe_load((ROOT / ".github/workflows/bump-base.yml").read_text())
    triggers = wf.get(True) or wf.get("on")
    assert "schedule" in triggers
    steps = "\n".join(s.get("run", "") for s in wf["jobs"]["bump"]["steps"])
    assert "scripts/bump-base.py" in steps
    assert "--label queued" in steps and "gh workflow run merge-train.yml" in steps
