"""The login name suggested from a person's full name, in the first-run
wizard and in the admin app's "Add account": one rule for both.

"John Doe" -> "jdoe", "John" -> "john", "Mary Ann Smith" -> "msmith":
the first letter of the first name and the whole last name, or the one
name alone. Always lowercase. Accents are dropped ("Élie" -> "elie"),
punctuation inside a name too ("O'Neill" -> "oneill", "Ben-David" ->
"bendavid"), and a name with no Latin letters gives nothing, so the person
types a username instead of being handed a wrong one.
"""

from __future__ import annotations

import re
import unicodedata

MAX_LENGTH = 32  # what useradd takes


def _plain(word: str) -> str:
    ascii_only = unicodedata.normalize("NFKD", word).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]", "", ascii_only.lower())


def suggest(full_name: str) -> str:
    words = [w for w in (_plain(w) for w in (full_name or "").split()) if w]
    # A login name cannot start with a digit.
    words = [w for w in (re.sub(r"^[0-9]+", "", w) for w in words) if w]
    if not words:
        return ""
    name = words[0] if len(words) == 1 else words[0][0] + words[-1]
    return name[:MAX_LENGTH]
