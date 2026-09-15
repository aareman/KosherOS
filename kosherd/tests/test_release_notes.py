"""Release notes a person reads instead of skims past.

The notes used to be a paragraph of boilerplate plus every commit subject
in one list; then they were headings and bullet lists over one line each,
which the user called "mostly meaningless". These pin what fixed that: a
first sentence saying what the build means for the reader; headings only
once there is enough to sort; housekeeping counted in one sentence and
never listed; each line filed as new, fixed or changed on its own words and
labelled with the part of the product it is about.
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


def test_docs_and_ci_work_is_counted_never_listed():
    grouped, quiet = rn.bullets([
        ("A page about what works", ["docs/supported.md"]),
        ("The workflow parses again", [".github/workflows/ci.yml"]),
        ("Ready-made approved-site lists", ["kosherd/src/kosherd/whitelists.py"]),
    ])
    assert quiet == ["Docs", "CI"]
    assert "quiet" not in grouped
    assert grouped["feat"] == ["**Filter** — Ready-made approved-site lists"]
    assert rn.housekeeping(quiet) == "Behind the scenes: two changes to CI and the docs."


def test_a_test_only_commit_is_housekeeping_however_it_reads():
    # "The release-always test follows the workflow: a failed image no
    # longer blocks it" was filed as a fix to Updates. It changed a test.
    grouped, quiet = rn.bullets([
        ("The release test follows the workflow: a failed image no longer blocks it",
         ["kosherd/tests/test_ci_workflow.py"]),
    ])
    assert not any(grouped.values())
    assert quiet == ["Tests"]


def fake_commits(monkeypatch, found):
    monkeypatch.setattr(rn, "commits", lambda previous, sha: found)


def test_the_first_line_says_what_the_build_means(monkeypatch):
    fake_commits(monkeypatch, [
        ("feat(store): categories on the home page; icons for every app",
         ["store-app/src/kosherstore/app.py"]),
        ("fix(admin): Enter submits the password", ["admin-app/src/kosheradmin/dialogs.py"]),
    ])
    text = rn.notes("0.1.0-pre.024", "v0.1.0-pre.023", "HEAD", "ghcr.io/x/k", "yes")
    assert text.startswith("**Two new things and one fix.**\n")
    assert "## ✨ New" in text and "## 🔧 Fixed" in text
    assert "- **Store** — Categories on the home page" in text
    assert "- **Store** — Icons for every app" in text
    assert "- **Admin** — Enter submits the password" in text
    assert "bootc switch ghcr.io/x/k:v0.1.0-pre.024" in text
    assert "edge" in text and "stable" in text
    assert "Since **v0.1.0-pre.023**." in text


def test_one_change_is_one_line_with_no_headings(monkeypatch):
    fake_commits(monkeypatch, [
        ("fix(admin): the guest card is the same size as the others",
         ["admin-app/src/kosheradmin/family.py"]),
    ])
    text = rn.notes("0.1.0-pre.024", "v0.1.0-pre.023", "HEAD", "ghcr.io/x/k", "no")
    assert text.startswith("**One fix.**\n")
    assert "## " not in text and "\n- " not in text
    assert "🔧 **Admin** — The guest card is the same size as the others" in text
    assert "No new image" in text


def test_a_build_of_housekeeping_says_nothing_changes_for_a_machine(monkeypatch):
    # The case the user was looking at: a big heading, one bullet, a
    # paragraph about the missing image, for a docs edit.
    fake_commits(monkeypatch, [
        ("The site wears the artwork's cream", ["docs/stylesheets/brand.css", "mkdocs.yml"]),
    ])
    text = rn.notes("0.1.0-pre.030", "v0.1.0-pre.029", "HEAD", "ghcr.io/x/k", "no")
    assert text.startswith("**Nothing on a KosherOS machine changes in this build.**\n")
    assert "## " not in text and "\n- " not in text
    assert "Behind the scenes: one change to the docs." in text
    assert "No new image" not in text, "already said: nothing changes"
    assert text.count("\n\n") <= 2, "two short paragraphs, not a page"


def test_a_failed_image_is_said_once_in_one_line(monkeypatch):
    fake_commits(monkeypatch, [("feat(store): shelves", ["store-app/src/kosherstore/app.py"])])
    text = rn.notes("0.1.0-pre.024", "", "HEAD", "ghcr.io/x/k", "no", image_failed=True)
    assert "⚠️ The image build failed" in text
    assert "## " not in text  # one feature: one line


def test_headings_only_appear_for_the_kinds_present(monkeypatch):
    fake_commits(monkeypatch, [
        ("feat(store): shelves", ["store-app/src/kosherstore/app.py"]),
        ("feat(admin): a guest can be given any mode", ["admin-app/src/kosheradmin/family.py"]),
    ])
    text = rn.notes("0.1.0", "", "HEAD", "ghcr.io/x/k", "no")
    assert "## ✨ New" in text
    assert "Fixed" not in text and "Changed" not in text and "Behind the scenes" not in text


def test_the_sections_are_ordered_new_then_fixed_then_changed():
    assert [key for key, _heading in rn.SECTIONS] == ["feat", "fix", "change"]
    headings = [heading for _key, heading in rn.SECTIONS]
    assert headings[0].endswith("New") and "Fixed" in headings[1]
    assert all(h.startswith("## ") for h in headings)


def test_a_long_list_is_capped_rather_than_endless(monkeypatch):
    many = [(f"Thing number {i}", ["kosherd/src/kosherd/dns.py"]) for i in range(30)]
    grouped, quiet = rn.bullets(many)
    assert sum(len(v) for v in grouped.values()) == 30 and not quiet
    fake_commits(monkeypatch, many)
    text = rn.notes("0.1.0", "", "HEAD", "ghcr.io/x/k", "no")
    assert rn.MAX_PER_SECTION <= 10
    assert text.count("\n- ") == rn.MAX_PER_SECTION + 1
    assert f"…and {30 - rn.MAX_PER_SECTION} more" in text


def test_counts_are_words_a_person_says():
    assert rn.count(1, "fix", "fixes") == "one fix"
    assert rn.count(3, "new thing", "new things") == "three new things"
    assert rn.count(14, "change", "changes") == "14 changes"
    assert rn.join(["a", "b", "c"]) == "a, b and c"


def test_the_workflow_uses_the_script():
    ci = (ROOT / ".github/workflows/ci.yml").read_text()
    assert "scripts/release-notes.py" in ci
    assert "--out release-notes.md" in ci
    assert "--notes-file release-notes.md" in ci
