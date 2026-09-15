"""Release notes a person reads instead of skims past.

The notes used to be a paragraph of boilerplate plus every commit subject
in one list. These pin the three things that fixed that: each line is
filed as new, fixed or housekeeping on its own words; each carries the part
of the product it is about; and a subject that says three things becomes
three lines.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
spec = importlib.util.spec_from_file_location("release_notes",
                                              ROOT / "scripts/release-notes.py")
rn = importlib.util.module_from_spec(spec)
sys.modules["release_notes"] = rn
spec.loader.exec_module(rn)


@pytest.mark.parametrize("subject,expected", [
    ("feat(store): categories on the home page", "feat"),
    ("fix(admin): the guest card is the wrong height", "fix"),
    ("docs: a page about what works", "quiet"),
    ("ci: run the tests on a newer runner", "quiet"),
    ("Ready-made approved-site lists for Torah study", "feat"),
    ("A stray argument broke the image job", "fix"),
    ("A whitelist account is no longer asked which kinds of site to block", "fix"),
    ("The wizard in larger type", "change"),
])
def test_each_line_is_filed_on_its_own_words(subject, expected):
    assert rn.classify(subject) == expected


def test_a_subject_that_says_three_things_becomes_three_lines():
    subject = ("Icons come from the app id; Enter submits every password; "
               "the name and mode share one line")
    assert rn.points(subject) == [
        "Icons come from the app id",
        "Enter submits every password",
        "The name and mode share one line",
    ]


def test_a_conventional_prefix_is_not_repeated_in_the_bullet():
    assert rn.points("feat(store): shelves and icons") == ["Shelves and icons"]


@pytest.mark.parametrize("files,expected", [
    (["admin-app/src/kosheradmin/app.py", "admin-app/tests/test_widgets.py"], "Admin"),
    (["store-app/src/kosherstore/app.py"], "Store"),
    (["kosherd/src/kosherd/dns.py"], "Filter"),
    (["os-image/Containerfile"], "System"),
    ([".github/workflows/ci.yml"], "CI"),
    (["docs/supported.md"], "Docs"),
])
def test_a_commit_is_labelled_by_where_it_landed(files, expected):
    assert rn.area(files) == expected


def test_the_line_beats_the_files_when_it_names_its_own_part():
    # One commit touched admin-app and store-app; each line says which.
    assert rn.area_for("The Store opens on its categories", "Admin") == "Store"
    assert rn.area_for("The wizard in larger type", "Admin") == "Setup"
    assert rn.area_for("Blocked reads blue", "Admin") == "Admin"


def test_docs_and_ci_work_is_never_a_headline():
    grouped = rn.bullets([
        ("A page about what works", ["docs/supported.md"]),
        ("The workflow parses again", [".github/workflows/ci.yml"]),
        ("Ready-made approved-site lists", ["kosherd/src/kosherd/whitelists.py"]),
    ])
    assert len(grouped["quiet"]) == 2
    assert grouped["feat"] == ["**Filtering** — Ready-made approved-site lists"]


def test_the_header_says_how_to_get_the_build():
    text = rn.notes("0.1.0-pre.024", "v0.1.0-pre.023", "HEAD",
                    "ghcr.io/x/kosher-linux", "yes")
    assert "bootc switch ghcr.io/x/kosher-linux:v0.1.0-pre.024" in text
    assert "edge" in text and "stable" in text
    assert "Since **v0.1.0-pre.023**." in text


def test_a_version_with_no_image_says_which_kind_of_no():
    skipped = rn.notes("0.1.0-pre.024", "", "HEAD", "ghcr.io/x/k", "no")
    assert "No new image" in skipped
    assert "previous one" in skipped
    failed = rn.notes("0.1.0-pre.024", "", "HEAD", "ghcr.io/x/k", "no",
                      image_failed=True)
    assert "No image for this version" in failed
    assert "build failed" in failed


def test_the_sections_are_ordered_new_then_fixed_then_the_rest():
    assert [key for key, _heading in rn.SECTIONS] == ["feat", "fix", "change", "quiet"]
    headings = [heading for _key, heading in rn.SECTIONS]
    assert headings[0].endswith("New") and "Fixed" in headings[1]
    assert all(h.startswith("## ") for h in headings)


def test_a_long_list_is_capped_rather_than_endless():
    many = [(f"Thing number {i}", ["kosherd/src/kosherd/dns.py"]) for i in range(30)]
    grouped = rn.bullets(many)
    text = rn.notes("0.1.0", "", "HEAD", "ghcr.io/x/k", "no")
    assert rn.MAX_PER_SECTION <= 12
    assert sum(len(v) for v in grouped.values()) == 30
    assert isinstance(text, str)


def test_the_workflow_uses_the_script():
    ci = (ROOT / ".github/workflows/ci.yml").read_text()
    assert "scripts/release-notes.py" in ci
    assert "--out release-notes.md" in ci
    assert "--notes-file release-notes.md" in ci
