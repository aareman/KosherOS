"""The "filter guardian" second password.

When guardian mode is enabled, filter-weakening actions require this password
in addition to the admin's own polkit authentication — dual control, so e.g.
the parent doing day-to-day admin cannot silently change filter modes without
the other parent's password.

The hash lives in /etc/kosher/guardian.shadow (root:root 0600), yescrypt via
libxcrypt — the same algorithm and library the system's /etc/shadow uses.
Python's stdlib crypt module was removed in 3.13, so we call libcrypt directly.

The count of wrong guesses is kept under /var/lib/kosher, kosherd's state
directory, so restarting the computer neither clears a lockout nor starts a
fresh round of guesses (F13 in #33). The times in it are wall-clock, since a
monotonic clock starts again at every boot. A clock moved back cannot stretch
a lockout past LOCKOUT_SECONDS, and wrong guesses older than
FAILURE_WINDOW_SECONDS stop counting.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import hmac
import json
import os
import time
from pathlib import Path

SHADOW_PATH = Path("/etc/kosher/guardian.shadow")
STATE_PATH = Path("/var/lib/kosher/guardian_attempts.json")

MAX_FAILURES = 5
LOCKOUT_SECONDS = 15 * 60
# Wrong guesses further apart than this are not one attempt at guessing.
FAILURE_WINDOW_SECONDS = 24 * 60 * 60


class GuardianError(Exception):
    pass


class GuardianLockedOut(GuardianError):
    pass


def _libcrypt() -> ctypes.CDLL:
    # KOSHERD_LIBCRYPT lets sandboxed dev environments (nix/devenv) point at
    # their own libxcrypt; production Fedora resolves libcrypt.so.2 normally.
    name = (
        os.environ.get("KOSHERD_LIBCRYPT")
        or ctypes.util.find_library("crypt")
        or "libcrypt.so.2"
    )
    lib = ctypes.CDLL(name, use_errno=True)
    lib.crypt.restype = ctypes.c_char_p
    lib.crypt.argtypes = [ctypes.c_char_p, ctypes.c_char_p]
    lib.crypt_gensalt.restype = ctypes.c_char_p
    lib.crypt_gensalt.argtypes = [ctypes.c_char_p, ctypes.c_ulong, ctypes.c_char_p, ctypes.c_int]
    return lib


def hash_password(password: str) -> str:
    lib = _libcrypt()
    setting = lib.crypt_gensalt(b"$y$", 0, None, 0)
    if not setting:
        raise GuardianError("libcrypt cannot generate a yescrypt setting")
    h = lib.crypt(password.encode(), setting)
    if not h or h.startswith(b"*"):
        raise GuardianError("password hashing failed")
    return h.decode()


def _check_hash(password: str, stored: str) -> bool:
    h = _libcrypt().crypt(password.encode(), stored.encode())
    return bool(h) and hmac.compare_digest(h.decode(), stored)


class Guardian:
    """Stateful verifier with rate limiting. Paths injectable for tests."""

    def __init__(self, shadow_path: Path = SHADOW_PATH, state_path: Path = STATE_PATH):
        self.shadow_path = shadow_path
        self.state_path = state_path

    @property
    def is_set(self) -> bool:
        return self.shadow_path.exists()

    def set_password(self, new_password: str, old_password: str | None = None) -> None:
        """Set/replace the guardian password. Replacing requires the old one."""
        if len(new_password) < 6:
            raise GuardianError("guardian password must be at least 6 characters")
        if self.is_set and not self.verify(old_password or ""):
            raise GuardianError("current guardian password is incorrect")
        self.shadow_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.shadow_path.with_suffix(".tmp")
        tmp.write_text(hash_password(new_password) + "\n")
        os.chmod(tmp, 0o600)
        os.rename(tmp, self.shadow_path)
        self._write_state(_cleared())

    def verify(self, password: str) -> bool:
        """Check the password; raises GuardianLockedOut during a lockout window."""
        if not self.is_set:
            raise GuardianError("no guardian password is set")
        now = time.time()
        state = self._read_state(now)
        if state["locked_until"] > now:
            # Kept as read, so an end brought forward by a clock set back
            # stays brought forward instead of moving with the clock.
            self._write_state(state)
            raise GuardianLockedOut(
                f"too many failed attempts; locked for {int(state['locked_until'] - now)}s"
            )
        ok = _check_hash(password, self.shadow_path.read_text().strip())
        if ok:
            self._write_state(_cleared())
        else:
            if not state["failures"]:
                state["first_failure"] = now
            state["failures"] += 1
            if state["failures"] >= MAX_FAILURES:
                state = {**_cleared(), "locked_until": now + LOCKOUT_SECONDS}
            self._write_state(state)
        return ok

    def _read_state(self, now: float) -> dict:
        """The stored count, with whatever a moved clock or a damaged file
        made of it brought back to sane values."""
        try:
            raw = json.loads(self.state_path.read_text())
            state = {
                "failures": max(0, int(raw.get("failures", 0))),
                "first_failure": float(raw.get("first_failure", 0)),
                "locked_until": float(raw.get("locked_until", 0)),
            }
        except (OSError, ValueError, TypeError, AttributeError):
            return _cleared()
        # Never locked for longer than LOCKOUT_SECONDS from now, so a clock
        # set back cannot keep the guardian out for good.
        state["locked_until"] = min(state["locked_until"], now + LOCKOUT_SECONDS)
        # A first failure in the future means the clock went back; its
        # window starts again from now.
        state["first_failure"] = min(state["first_failure"], now)
        if state["failures"] and now - state["first_failure"] > FAILURE_WINDOW_SECONDS:
            state["failures"] = 0
            state["first_failure"] = 0
        return state

    def _write_state(self, state: dict) -> None:
        # A temporary file renamed into place, so a power cut part-way
        # leaves the previous count rather than an empty file read as none.
        self.state_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        tmp = self.state_path.with_name(self.state_path.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps(state))
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.state_path)


def _cleared() -> dict:
    return {"failures": 0, "first_failure": 0, "locked_until": 0}
