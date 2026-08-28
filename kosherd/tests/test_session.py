from kosherd.session import SessionStore


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


def store(ttl=900):
    clock = FakeClock()
    return SessionStore(ttl=ttl, clock=clock), clock


def test_locked_until_unlocked():
    s, _ = store()
    assert not s.is_unlocked(1000)
    s.unlock(1000)
    assert s.is_unlocked(1000)


def test_sessions_are_per_uid():
    s, _ = store()
    s.unlock(1000)
    assert not s.is_unlocked(1001)


def test_expires_after_ttl_of_inactivity():
    s, clock = store(ttl=900)
    s.unlock(1000)
    clock.advance(901)
    assert not s.is_unlocked(1000)


def test_activity_slides_the_window():
    s, clock = store(ttl=900)
    s.unlock(1000)
    for _ in range(5):
        clock.advance(800)
        assert s.is_unlocked(1000)  # each check refreshes
    clock.advance(901)
    assert not s.is_unlocked(1000)


def test_lock_clears_session_and_guardian():
    s, _ = store()
    s.unlock(1000)
    s.grant_guardian(1000)
    s.lock(1000)
    assert not s.is_unlocked(1000)
    assert not s.has_guardian(1000)


def test_guardian_proof_expires_and_dies_with_session():
    s, clock = store(ttl=900)
    s.unlock(1000)
    s.grant_guardian(1000)
    assert s.has_guardian(1000)
    clock.advance(901)
    assert not s.is_unlocked(1000)   # expiring the session...
    assert not s.has_guardian(1000)  # ...also drops guardian proof


def test_seconds_remaining():
    s, clock = store(ttl=900)
    assert s.seconds_remaining(1000) == 0
    s.unlock(1000)
    clock.advance(300)
    assert s.seconds_remaining(1000) == 600
