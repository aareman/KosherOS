"""The polkit rules for KosherOS's own actions, and who is an administrator.

An administrator signed in at the computer is never asked for a password —
"its my account". Anyone else at the keyboard may unlock the admin tools
with an administrator's password, kept for the sitting; and that is the
only thing the kosher-admin group can authorize as polkit administrators,
so every stock privileged action stays unsatisfiable.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).parents[2]
RULES = ROOT / "os-image/files/etc/polkit-1/rules.d"
NO_ADMIN = RULES / "40-kosher-no-admin.rules"
ADMIN = RULES / "49-kosher-admin.rules"


def _branch(text: str, start: str, end: str) -> str:
    return text[text.index(start):text.index(end, text.index(start))]


def test_the_admins_own_session_is_never_asked_for_a_password():
    text = ADMIN.read_text()
    assert "AUTH_SELF_KEEP" not in text
    admins = _branch(text, 'subject.isInGroup("kosher-admin")', "}")
    assert "polkit.Result.YES" in admins


def test_anyone_else_is_asked_for_an_administrators_password_and_it_is_kept():
    text = ADMIN.read_text()
    assert "AUTH_ADMIN_KEEP" in text, "kept for the sitting, not asked per change"
    # The prompt is the last answer: it catches only what the branches
    # above it did not — an administrator's session, the free reads.
    assert text.rindex("AUTH_ADMIN_KEEP") > text.rindex("polkit.Result.YES")
    for free in ("org.kosherlinux.use-store", "org.kosherlinux.read-own-settings"):
        assert free in text, free


def test_nothing_is_reachable_from_a_remote_or_inactive_session():
    text = ADMIN.read_text()
    assert "!(subject.active && subject.local)" in text
    assert text.index("NOT_HANDLED") < text.index("polkit.Result.YES")


def test_kosher_admins_are_administrators_only_for_kosheros_actions():
    text = NO_ADMIN.read_text()
    kosher = _branch(text, 'action.id.indexOf("org.kosherlinux.") === 0', "}")
    assert '"unix-group:kosher-admin"' in kosher
    # Everything else still has nobody who could authorize it.
    assert re.search(r"return \[\];\s*\}\);\s*$", text), "stock actions: no admin identities"
    assert text.count("unix-group") == 1


def test_the_admin_app_lets_another_account_try_with_an_admins_password():
    src = (ROOT / "admin-app/src/kosheradmin/app.py").read_text()
    unlock = _branch(src, "def _unlock(self)", "def lock(self)")
    assert "administrator's password" in unlock
    assert "cannot change settings here" not in unlock, "no dead end: polkit prompts"
    # The unlock call is made whether or not the account is an admin.
    assert unlock.count("self.client.unlock") == 1
    assert "return\n" not in unlock.split("def on_done")[0]
