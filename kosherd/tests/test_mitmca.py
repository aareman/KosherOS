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
    bundle = tmp_path / "extracted" / "tls-ca-bundle.pem"
    bundle.parent.mkdir()
    bundle.write_text("-----ROOTS-----\n")
    monkeypatch.setattr(mitmca, "SYSTEM_BUNDLE", bundle)
    monkeypatch.setattr(mitmca, "FLATPAK_CA_DIR", tmp_path / "kosher-ca")
    monkeypatch.setattr(mitmca, "FLATPAK_BUNDLE", tmp_path / "kosher-ca" / "tls-ca-bundle.pem")
    monkeypatch.setattr(mitmca, "FLATPAK_OVERRIDE", tmp_path / "overrides" / "global")

    def generate(argv):
        # mitmdump writes its CA on first run; imitate that.
        if "mitmdump" in argv[0]:
            mitm_dir.mkdir(parents=True, exist_ok=True)
            (mitm_dir / "mitmproxy-ca-cert.pem").write_text("-----FAKE CA-----\n")
        # update-ca-trust puts the anchors into the extracted bundle.
        if argv[0] == "update-ca-trust":
            anchor = mitmca.ANCHOR.read_text() if mitmca.ANCHOR.exists() else ""
            bundle.write_text("-----ROOTS-----\n" + anchor)

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


# ---- Flatpak apps (#223) --------------------------------------------------------------

def test_flatpak_apps_get_a_copy_of_the_bundle_with_the_ca_in_it(ca):
    mitmca.ensure_ca()
    assert "FAKE CA" in mitmca.FLATPAK_BUNDLE.read_text()
    assert mitmca.FLATPAK_BUNDLE.read_bytes() == mitmca.SYSTEM_BUNDLE.read_bytes()
    assert oct(mitmca.FLATPAK_BUNDLE.stat().st_mode & 0o777) == "0o644"
    assert oct(mitmca.FLATPAK_CA_DIR.stat().st_mode & 0o777) == "0o755"


def test_the_override_shares_the_copy_and_points_every_variable_at_it(ca):
    mitmca.ensure_ca()
    text = mitmca.FLATPAK_OVERRIDE.read_text()
    assert f"filesystems={mitmca.FLATPAK_CA_DIR}:ro;" in text
    for name in mitmca.FLATPAK_ENV:
        assert f"{name}={mitmca.FLATPAK_BUNDLE}" in text


def test_every_bundle_variable_the_session_sets_is_pointed_at_the_copy():
    from pathlib import Path

    conf = Path(__file__).parents[2] / "os-image/files/etc/environment.d/50-kosher-ca.conf"
    names = {line.split("=", 1)[0] for line in conf.read_text().splitlines()
             if line.endswith("tls-ca-bundle.pem")}
    assert names and names == set(mitmca.FLATPAK_ENV)


def test_a_new_ca_reaches_the_copy(ca):
    mitmca.ensure_ca()
    mitmca.CA_PEM.write_text("-----NEW CA-----\n")
    mitmca.ensure_ca()
    assert "NEW CA" in mitmca.FLATPAK_BUNDLE.read_text()


def test_removing_trust_takes_the_ca_out_of_the_copy_too(ca):
    mitmca.ensure_ca()
    mitmca.remove_trust()
    assert "FAKE CA" not in mitmca.FLATPAK_BUNDLE.read_text()


def test_an_existing_override_keeps_what_else_it_says():
    existing = ("# the administrator's own\n[Context]\nfilesystems=xdg-download;!home;\n"
                "sockets=wayland;\n\n[Environment]\nFOO=bar\nSSL_CERT_FILE=/old\n"
                "\n[Session Bus Policy]\norg.example=talk\n")
    out = mitmca.render_override(existing)
    assert "# the administrator's own" in out
    assert f"filesystems=xdg-download;!home;{mitmca.FLATPAK_CA_DIR}:ro;" in out
    assert "sockets=wayland;" in out and "FOO=bar" in out
    assert "[Session Bus Policy]\norg.example=talk" in out
    assert "SSL_CERT_FILE=/old" not in out
    assert [l for l in out.splitlines() if l.startswith("SSL_CERT_FILE=")] == [
        f"SSL_CERT_FILE={mitmca.FLATPAK_BUNDLE}"]


def test_rendering_twice_changes_nothing():
    once = mitmca.render_override("")
    assert mitmca.render_override(once) == once
    assert once.startswith("[Context]\n")


def test_nothing_is_rewritten_when_nothing_changed(ca):
    mitmca.ensure_ca()
    assert mitmca.sync_flatpak() is False


def test_a_missing_copy_is_reported_in_words(ca):
    mitmca.ensure_ca()
    assert mitmca.installed() == (True, None)
    mitmca.FLATPAK_BUNDLE.unlink()
    ok, why = mitmca.installed()
    assert not ok and "apps from the Store" in why
