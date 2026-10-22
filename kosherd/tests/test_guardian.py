
import pytest

from kosherd import guardian
from kosherd.guardian import Guardian, GuardianError, GuardianLockedOut


def _yescrypt_available() -> bool:
    try:
        lib = guardian._libcrypt()
        return bool(lib.crypt_gensalt(b"$y$", 0, None, 0))
    except (OSError, AttributeError):
        return False


pytestmark = pytest.mark.skipif(
    not _yescrypt_available(), reason="libxcrypt with yescrypt not available"
)


@pytest.fixture
def g(tmp_path):
    return Guardian(shadow_path=tmp_path / "guardian.shadow", state_path=tmp_path / "state.json")


def test_set_and_verify(g):
    g.set_password("s3cret-pw")
    assert g.verify("s3cret-pw")
    assert not g.verify("wrong")
    assert g.shadow_path.read_text().startswith("$y$")
    assert (g.shadow_path.stat().st_mode & 0o777) == 0o600


def test_replace_requires_old_password(g):
    g.set_password("first-pw")
    with pytest.raises(GuardianError):
        g.set_password("second-pw", old_password="wrong")
    g.set_password("second-pw", old_password="first-pw")
    assert g.verify("second-pw")


def test_short_password_rejected(g):
    with pytest.raises(GuardianError):
        g.set_password("abc")


def test_verify_without_password_set(g):
    with pytest.raises(GuardianError):
        g.verify("anything")


def test_lockout_after_max_failures(g):
    g.set_password("s3cret-pw")
    for _ in range(guardian.MAX_FAILURES):
        g.verify("wrong")
    with pytest.raises(GuardianLockedOut):
        g.verify("s3cret-pw")


def test_success_resets_failure_count(g):
    g.set_password("s3cret-pw")
    for _ in range(guardian.MAX_FAILURES - 1):
        g.verify("wrong")
    assert g.verify("s3cret-pw")
    # counter reset: another burst of failures is needed to lock
    for _ in range(guardian.MAX_FAILURES - 1):
        g.verify("wrong")
    assert g.verify("s3cret-pw")


# F13 (#33, #188): the count of wrong guesses was kept in /run, a tmpfs, so
# restarting the computer cleared a lockout and gave a fresh round of
# guesses. It is kept with kosherd's state now.
def test_state_is_kept_across_reboots():
    assert str(guardian.STATE_PATH).startswith("/var/lib/kosher/")


def test_lockout_survives_a_new_guardian_object(g, tmp_path):
    # A restarted daemon (or a rebooted machine) reads the same file.
    g.set_password("s3cret-pw")
    for _ in range(guardian.MAX_FAILURES):
        g.verify("wrong")
    again = Guardian(shadow_path=g.shadow_path, state_path=g.state_path)
    with pytest.raises(GuardianLockedOut):
        again.verify("s3cret-pw")


def test_failures_survive_a_new_guardian_object(g):
    g.set_password("s3cret-pw")
    for _ in range(guardian.MAX_FAILURES - 1):
        g.verify("wrong")
    again = Guardian(shadow_path=g.shadow_path, state_path=g.state_path)
    again.verify("wrong")
    with pytest.raises(GuardianLockedOut):
        again.verify("s3cret-pw")


def test_lockout_ends(g, monkeypatch):
    g.set_password("s3cret-pw")
    now = [1_000_000.0]
    monkeypatch.setattr(guardian.time, "time", lambda: now[0])
    for _ in range(guardian.MAX_FAILURES):
        g.verify("wrong")
    now[0] += guardian.LOCKOUT_SECONDS + 1
    assert g.verify("s3cret-pw")


def test_clock_set_back_cannot_lock_out_for_good(g, monkeypatch):
    g.set_password("s3cret-pw")
    now = [2_000_000_000.0]
    monkeypatch.setattr(guardian.time, "time", lambda: now[0])
    for _ in range(guardian.MAX_FAILURES):
        g.verify("wrong")
    now[0] -= 365 * 24 * 3600  # the clock goes back a year
    with pytest.raises(GuardianLockedOut):
        g.verify("s3cret-pw")
    now[0] += guardian.LOCKOUT_SECONDS + 1
    assert g.verify("s3cret-pw")


def test_old_failures_stop_counting(g, monkeypatch):
    g.set_password("s3cret-pw")
    now = [1_000_000.0]
    monkeypatch.setattr(guardian.time, "time", lambda: now[0])
    for _ in range(guardian.MAX_FAILURES - 1):
        g.verify("wrong")
    now[0] += guardian.FAILURE_WINDOW_SECONDS + 1
    g.verify("wrong")  # one fresh mistake does not lock
    assert g.verify("s3cret-pw")


def test_damaged_state_reads_as_none(g):
    g.set_password("s3cret-pw")
    for junk in ("", "[]", '{"failures": "x"}', "not json"):
        g.state_path.write_text(junk)
        assert g.verify("s3cret-pw")


def test_state_file_is_root_only(g):
    g.set_password("s3cret-pw")
    g.verify("wrong")
    assert (g.state_path.stat().st_mode & 0o777) == 0o600
    assert not list(g.state_path.parent.glob("*.tmp"))
