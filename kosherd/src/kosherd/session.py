"""Admin and guardian sessions: authenticate once, then work.

Per-change polkit prompts make the admin app unusable, so the app calls
Unlock() once and kosherd keeps a sliding session for that uid. While it is
alive, that uid's calls skip the polkit check. For an administrator's own
signed-in session polkit answers yes without a prompt (49-kosher-admin.rules);
on any other account Unlock is the one prompt, for an administrator's
password, and this session is what turns it into a sliding window.

Guardian dual-control works the same way: the password is verified once and
kept for the same sliding window, so a filter change is not a password quiz.

Sessions are per-uid, in-memory only (a daemon restart or reboot clears
them), and the TTL slides on each authorized call.
"""

from __future__ import annotations

import time

DEFAULT_TTL_SECONDS = 15 * 60


class SessionStore:
    def __init__(self, ttl: float = DEFAULT_TTL_SECONDS, clock=time.monotonic):
        self.ttl = ttl
        self._clock = clock
        self._sessions: dict[int, float] = {}   # uid -> last activity
        self._guardian: dict[int, float] = {}   # uid -> last guardian proof

    # -- admin session -----------------------------------------------------

    def unlock(self, uid: int) -> None:
        self._sessions[uid] = self._clock()

    def is_unlocked(self, uid: int) -> bool:
        """True if uid has a live session; refreshes it (sliding window)."""
        last = self._sessions.get(uid)
        if last is None:
            return False
        if self._clock() - last > self.ttl:
            del self._sessions[uid]
            self._guardian.pop(uid, None)  # guardian never outlives the session
            return False
        self._sessions[uid] = self._clock()
        return True

    def lock(self, uid: int) -> None:
        self._sessions.pop(uid, None)
        self._guardian.pop(uid, None)

    # -- guardian proof ----------------------------------------------------

    def grant_guardian(self, uid: int) -> None:
        self._guardian[uid] = self._clock()

    def has_guardian(self, uid: int) -> bool:
        last = self._guardian.get(uid)
        if last is None:
            return False
        if self._clock() - last > self.ttl:
            del self._guardian[uid]
            return False
        self._guardian[uid] = self._clock()
        return True

    def seconds_remaining(self, uid: int) -> float:
        last = self._sessions.get(uid)
        return 0.0 if last is None else max(0.0, self.ttl - (self._clock() - last))
