"""Whether the filter is actually holding the lists it thinks it has.

Every list loader here fails open and quiet: miss the category database
and the bundle is empty, miss the word list and the profanity filter is
off. That behaviour is right — a missing file must not take the machine
down — and the silence is what is wrong, because the account still says
"Filtered internet" while nothing is filtered.
"""

import pytest

from kosherd import selfcheck


def test_a_list_that_did_not_load_is_reported_as_a_fault():
    found = [{"name": "wordlist", "label": "bad-language list",
              "entries": 0, "source": None, "ok": False}]
    said = selfcheck.problems(found)
    assert len(said) == 1
    assert "did not load" in said[0]
    assert "bad-language list" in said[0]


def test_a_truncated_list_says_so_rather_than_looking_healthy():
    # Half a list is the worse failure: it looks like it is working.
    found = [{"name": "categories", "label": "list of classified sites",
              "entries": 12, "source": "/x", "ok": False}]
    said = selfcheck.problems(found)
    assert "truncated or damaged" in said[0]
    assert "12" in said[0]


def test_healthy_lists_say_nothing():
    found = [{"name": "categories", "label": "x", "entries": 5_000_000,
              "source": "/x", "ok": True}]
    assert selfcheck.problems(found) == []


def test_every_list_has_words_for_a_person():
    # "the wordlist list did not load" is what you get for free, and it is
    # not a sentence anyone should have to read.
    assert set(selfcheck.LABELS) == set(selfcheck.MINIMUM)
    for label in selfcheck.LABELS.values():
        assert label.islower() and " " in label


def test_the_floors_are_far_below_what_ships():
    # A floor set just under the shipped size would fire on every ordinary
    # edit; these exist to catch empty and truncated, nothing else.
    from pathlib import Path

    root = Path(__file__).parents[2] / "os-image/files/usr/share/kosher"
    import json
    shipped = {
        "content-terms": sum(
            len(v) for by_level in json.loads(
                (root / "content-terms.json").read_text())["terms"].values()
            for v in by_level.values()),
        "wordlist": len(json.loads(
            (root / "wordlist.json").read_text())["replacements"]),
        "search-blocklist": len(json.loads(
            (root / "search-blocklist.json").read_text())["terms"]),
    }
    for name, size in shipped.items():
        assert selfcheck.MINIMUM[name] < size / 2, name


def test_checking_a_list_cannot_be_the_thing_that_breaks(monkeypatch):
    def explode():
        raise RuntimeError("no")

    monkeypatch.setattr(selfcheck, "_count", lambda name: explode())
    found = selfcheck.datasets()
    assert len(found) == len(selfcheck.MINIMUM)
    assert all(entry["entries"] == 0 and not entry["ok"] for entry in found)
