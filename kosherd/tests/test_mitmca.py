"""The inspection CA: generated once, trusted by the system and by Firefox.

If this goes wrong the symptom is every HTTPS page warning, so the checks
here are about idempotence and about actually refreshing trust when the CA
changes.
"""

import pytest

from kosherd import mitmca
from kosherd.mitmca import CAError


class FakeRun:
    def __init__(self, on_call=None, fail=()):
        self.calls: list[list[str]] = []
        self.on_call = on_call
        self.fail = fail

    def __call__(self, argv, **kwargs):
        import subprocess

        self.calls.append(list(argv))
        if self.on_call:
            self.on_call(argv)
        failed = any(marker in " ".join(argv) for marker in self.fail)
        return subprocess.CompletedProcess(
            argv, 1 if failed else 0, stdout="", stderr="boom" if failed else "")

    def ran(self, needle) -> bool:
        return any(needle in " ".join(c) for c in self.calls)

    def count(self, needle) -> int:
        return sum(1 for c in self.calls if needle in " ".join(c))


@pytest.fixture
def ca(tmp_path, monkeypatch):
    """mitmca pointed at temp paths, with a stand-in for mitmdump."""
    mitm_dir = tmp_path / "mitm"
    monkeypatch.setattr(mitmca, "MITM_DIR", mitm_dir)
    monkeypatch.setattr(mitmca, "CA_PEM", mitm_dir / "mitmproxy-ca-cert.pem")
    monkeypatch.setattr(mitmca, "ANCHOR", tmp_path / "anchors" / "kosheros.crt")
    monkeypatch.setattr(mitmca, "FIREFOX_POLICY_DIR", tmp_path / "firefox")
    monkeypatch.setattr(mitmca, "FIREFOX_POLICY", tmp_path / "firefox" / "policies.json")

    def generate(argv):
        # mitmdump writes its CA on first run; imitate that.
        if "mitmdump" in argv[0]:
            mitm_dir.mkdir(parents=True, exist_ok=True)
            (mitm_dir / "mitmproxy-ca-cert.pem").write_text("-----FAKE CA-----\n")

    run = FakeRun(on_call=generate)
    monkeypatch.setattr(mitmca.subprocess, "run", run)
    return run


def test_generates_the_ca_when_missing(ca):
    mitmca.ensure_ca()
    assert mitmca.CA_PEM.exists()
    assert ca.ran("mitmdump")


def test_installs_the_ca_into_the_system_trust_store(ca):
    mitmca.ensure_ca()
    assert mitmca.ANCHOR.read_text() == mitmca.CA_PEM.read_text()
    assert ca.ran("update-ca-trust")


def test_key_material_is_owned_by_the_proxy_user(ca):
    # The proxy runs unprivileged and must be able to read its own key.
    mitmca.ensure_ca()
    assert ca.ran("chown -R kosher-mitm:kosher-mitm") or any(
        c[:2] == ["chown", "-R"] and "kosher-mitm:kosher-mitm" in c for c in ca.calls)


def test_enables_enterprise_roots_for_firefox(ca):
    mitmca.ensure_ca()
    import json

    policy = json.loads(mitmca.FIREFOX_POLICY.read_text())
    assert policy["policies"]["Certificates"]["ImportEnterpriseRoots"] is True


def test_is_idempotent(ca):
    mitmca.ensure_ca()
    first = ca.count("update-ca-trust")
    mitmca.ensure_ca()
    # Nothing changed, so trust is not rebuilt and no second CA is made.
    assert ca.count("update-ca-trust") == first
    assert ca.count("mitmdump") == 1


def test_refreshes_trust_when_the_ca_changes(ca):
    mitmca.ensure_ca()
    before = ca.count("update-ca-trust")
    mitmca.CA_PEM.write_text("-----DIFFERENT CA-----\n")
    mitmca.ensure_ca()
    assert ca.count("update-ca-trust") == before + 1
    assert mitmca.ANCHOR.read_text() == "-----DIFFERENT CA-----\n"


def test_an_existing_firefox_policy_is_left_alone(ca):
    mitmca.FIREFOX_POLICY_DIR.mkdir(parents=True)
    mitmca.FIREFOX_POLICY.write_text('{"policies": {"custom": true}}')
    mitmca.ensure_ca()
    assert "custom" in mitmca.FIREFOX_POLICY.read_text()


def test_reports_when_no_ca_could_be_generated(tmp_path, monkeypatch):
    monkeypatch.setattr(mitmca, "MITM_DIR", tmp_path / "mitm")
    monkeypatch.setattr(mitmca, "CA_PEM", tmp_path / "mitm" / "nope.pem")
    monkeypatch.setattr(mitmca.subprocess, "run", FakeRun())  # writes nothing
    with pytest.raises(CAError, match="did not create a CA"):
        mitmca.ensure_ca()


def test_reports_a_failing_trust_update(ca, monkeypatch):
    monkeypatch.setattr(mitmca.subprocess, "run",
                        FakeRun(on_call=ca.on_call, fail=["update-ca-trust"]))
    with pytest.raises(CAError, match="update-ca-trust failed"):
        mitmca.ensure_ca()


def test_remove_trust_deletes_the_anchor(ca):
    mitmca.ensure_ca()
    mitmca.remove_trust()
    assert not mitmca.ANCHOR.exists()
    assert ca.count("update-ca-trust") >= 2


def test_remove_trust_is_safe_when_nothing_is_installed(ca):
    mitmca.remove_trust()  # must not raise
