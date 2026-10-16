"""The username suggested from a full name (kosherd.usernames)."""

import re

import pytest

from kosherd import usernames


@pytest.mark.parametrize(("full", "expected"), [
    ("John Doe", "jdoe"),
    ("john doe", "jdoe"),
    ("JOHN DOE", "jdoe"),
    ("John", "john"),
    ("JOHN", "john"),
    ("Mary Ann Smith", "msmith"),
    ("  John   Doe\t", "jdoe"),
    ("Élie Cohen", "ecohen"),
    ("Anne-Marie O'Neill", "aoneill"),
    ("Elisha Ben-David", "ebendavid"),
    ("John 3rd", "jrd"),
    ("2Pac", "pac"),
    ("A " + "B" * 40, "a" + "b" * 31),
    ("", ""),
    ("   ", ""),
    ("123", ""),
    ("!!!", ""),
    ("אברהם כהן", ""),
])
def test_first_initial_and_last_name_or_the_one_name(full, expected):
    got = usernames.suggest(full)
    assert got == expected
    # Whatever it suggests is a login name the wizard accepts.
    assert not got or re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", got)


def test_a_hebrew_word_beside_a_latin_name_is_skipped():
    assert usernames.suggest("John כהן Doe") == "jdoe"
