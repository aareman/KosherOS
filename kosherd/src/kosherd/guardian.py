"""The "filter guardian" second password.

When guardian mode is enabled, filter-weakening actions require this password
in addition to the admin's own polkit authentication — dual control, so e.g.
the parent doing day-to-day admin cannot silently change filter modes without
the other parent's password.

The hash lives in /etc/kosher/guardian.shadow (root:root 0600), yescrypt via
libxcrypt — the same algorithm and library the system's /etc/shadow uses.
Python's stdlib crypt module was removed in 3.13, so we call libcrypt directly.
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
STATE_PATH = Path("/run/kosherd/guardian_attempts.json")

MAX_FAILURES = 5
LOCKOUT_SECONDS = 15 * 60


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
        self._write_state({"failures": 0, "locked_until": 0})

    def verify(self, password: str) -> bool:
        """Check the password; raises GuardianLockedOut during a lockout window."""
        if not self.is_set:
            raise GuardianError("no guardian password is set")
        state = self._read_state()
        now = time.time()
        if state["locked_until"] > now:
            raise GuardianLockedOut(
                f"too many failed attempts; locked for {int(state['locked_until'] - now)}s"
            )
        ok = _check_hash(password, self.shadow_path.read_text().strip())
        if ok:
            self._write_state({"failures": 0, "locked_until": 0})
        else:
            state["failures"] += 1
            if state["failures"] >= MAX_FAILURES:
                state = {"failures": 0, "locked_until": now + LOCKOUT_SECONDS}
            self._write_state(state)
        return ok

    def _read_state(self) -> dict:
        try:
            return json.loads(self.state_path.read_text())
        except (FileNotFoundError, ValueError):
            return {"failures": 0, "locked_until": 0}

    def _write_state(self, state: dict) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(state))
        os.chmod(self.state_path, 0o600)
