
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
