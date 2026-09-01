"""What is actually being enforced, as opposed to what is configured.

A filter that has quietly stopped doing something is worse than one that
never did it, because the family is relying on it. These tests are about
the difference between the two being visible.
"""

import json

import pytest

from kosherd import vision
from kosherd.daemon import Daemon
from kosherd.policy import Policy, UserPolicy


def _daemon(users, run, monkeypatch, status_path=None):
    monkeypatch.setattr("kosherd.daemon.subprocess.run", run)
    if status_path is not None:
        monkeypatch.setattr(vision, "STATUS_PATH", status_path)
    daemon = Daemon.__new__(Daemon)
    daemon.policy = Policy(revision=1, users=users)
    return json.loads(daemon.impl_FilterStatus().unpack()[0])


def _all(state):
    def run(argv, **kw):
        return type("R", (), {"stdout": state, "returncode": 0})()
    return run


def test_a_service_that_should_be_running_and_is_not_is_named(monkeypatch):
    users = [UserPolicy(uid=1001, username="a", mode="filtered")]
    status = _daemon(users, _all("failed\n"), monkeypatch)
    assert "kosher-mitm.service" in status["degraded"]
    assert "kosher-dns.service" in status["degraded"]


def test_nothing_is_degraded_when_everything_runs(monkeypatch):
    users = [UserPolicy(uid=1001, username="a", mode="filtered")]
    status = _daemon(users, _all("active\n"), monkeypatch)
    assert status["degraded"] == []


def test_the_proxy_being_down_only_matters_if_somebody_is_inspected(monkeypatch):
    # A house where nobody is in filtered mode does not need the proxy, and
    # reporting it as a fault would train people to ignore the report.
    users = [UserPolicy(uid=1001, username="a", mode="dnsfilter")]
    def run(argv, **kw):
        state = "failed" if "kosher-mitm.service" in argv else "active"
        return type("R", (), {"stdout": state, "returncode": 0})()
    status = _daemon(users, run, monkeypatch)
    assert "kosher-mitm.service" not in status["degraded"]


def test_the_picture_state_is_read_from_what_the_proxy_published(
        monkeypatch, tmp_path):
    path = tmp_path / "status.json"
    path.write_text(json.dumps({"pictures": vision.TOO_SLOW, "detect_ms": 620}))
    users = [UserPolicy(uid=1001, username="a", mode="filtered")]
    status = _daemon(users, _all("active\n"), monkeypatch, status_path=path)
    assert status["pictures"] == vision.TOO_SLOW
    assert status["detect_ms"] == 620


def test_no_published_status_is_unknown_rather_than_a_guess(
        monkeypatch, tmp_path):
    # Nothing has been judged yet, or nobody is in a mode that judges.
    # Neither is a fault, and claiming either way would be a lie.
    users = [UserPolicy(uid=1001, username="a", mode="filtered")]
    status = _daemon(users, _all("active\n"), monkeypatch,
                     status_path=tmp_path / "nope.json")
    assert status["pictures"] == "unknown"


# -- what the proxy publishes -------------------------------------------------

def _filter(tmp_path, ms, slow_ms=400, available=True):
    import time as _t

    class Detector:
        def __init__(self):
            self.available = available

        def detect(self, data):
            _t.sleep(ms / 1000)
            return []

    return vision.ImageFilter(detector=Detector(),
                              cache=vision.VerdictCache(tmp_path / "i.sqlite"),
                              slow_ms=slow_ms)


def test_a_healthy_machine_reports_that_it_is_checking(tmp_path):
    f = _filter(tmp_path, ms=1)
    f.verdict(b"x" * 10_000)
    assert f.status()["pictures"] == vision.CHECKING


def test_a_slow_machine_reports_why_pictures_are_hidden(tmp_path):
    f = _filter(tmp_path, ms=40, slow_ms=10)
    for i in range(vision.SLOW_WINDOW):
        f.verdict(b"x" * 10_000 + bytes([i % 251]))
    status = f.status()
    assert status["pictures"] == vision.TOO_SLOW
    assert status["detect_ms"] >= 40


def test_a_machine_with_no_model_says_so(tmp_path):
    f = vision.ImageFilter(detector=vision.NullDetector(),
                           cache=vision.VerdictCache(tmp_path / "i.sqlite"))
    assert f.status()["pictures"] == vision.NO_MODEL


def test_the_status_file_is_only_written_when_it_changes(tmp_path):
    # It is published from the response hook, so it must cost nothing on
    # the overwhelming majority of pictures.
    path = tmp_path / "status.json"
    f = _filter(tmp_path, ms=1)
    f.verdict(b"x" * 10_000)
    f.write_status(path)
    first = path.stat().st_mtime_ns
    for i in range(5):
        f.verdict(b"x" * 10_000 + bytes([i]))
        f.write_status(path)
    assert path.stat().st_mtime_ns == first
    assert json.loads(path.read_text())["pictures"] == vision.CHECKING


def test_a_change_of_state_is_published(tmp_path):
    path = tmp_path / "status.json"
    f = _filter(tmp_path, ms=40, slow_ms=10)
    f.verdict(b"x" * 10_000)
    f.write_status(path)
    assert json.loads(path.read_text())["pictures"] == vision.CHECKING
    for i in range(vision.SLOW_WINDOW):
        f.verdict(b"x" * 10_000 + bytes([i % 251]))
        f.write_status(path)
    assert json.loads(path.read_text())["pictures"] == vision.TOO_SLOW


def test_an_unwritable_status_path_is_not_a_crash(tmp_path):
    f = _filter(tmp_path, ms=1)
    f.verdict(b"x" * 10_000)
    f.write_status(tmp_path / "no" / "such" / "dir" / "s.json")


# -- the inspection certificate -----------------------------------------------

def test_a_missing_inspection_certificate_is_reported(monkeypatch, tmp_path):
    # The failure is severe and confusing: without the CA, every HTTPS page
    # a filtered account opens shows a certificate warning, and nothing
    # connects that to the filter.
    from kosherd import mitmca

    monkeypatch.setattr(mitmca, "CA_PEM", tmp_path / "nope.pem")
    monkeypatch.setattr(mitmca, "ANCHOR", tmp_path / "nope.crt")
    users = [UserPolicy(uid=1001, username="a", mode="filtered")]
    status = _daemon(users, _all("active\n"), monkeypatch)
    assert status["inspection_ca"] == "broken"
    assert any("certificate" in p for p in status["problems"])


def test_a_certificate_that_is_not_trusted_is_reported(monkeypatch, tmp_path):
    from kosherd import mitmca

    ca = tmp_path / "ca.pem"
    ca.write_bytes(b"cert")
    monkeypatch.setattr(mitmca, "CA_PEM", ca)
    monkeypatch.setattr(mitmca, "ANCHOR", tmp_path / "nope.crt")
    ok, why = mitmca.installed()
    assert not ok and "trust store" in why


def test_a_stale_trusted_copy_is_reported(monkeypatch, tmp_path):
    # Regenerating the CA without refreshing the anchor gives exactly the
    # same symptom as having none, and is harder to spot.
    from kosherd import mitmca

    ca = tmp_path / "ca.pem"
    anchor = tmp_path / "anchor.crt"
    ca.write_bytes(b"new")
    anchor.write_bytes(b"old")
    monkeypatch.setattr(mitmca, "CA_PEM", ca)
    monkeypatch.setattr(mitmca, "ANCHOR", anchor)
    ok, why = mitmca.installed()
    assert not ok and "does not match" in why


def test_a_healthy_certificate_says_nothing(monkeypatch, tmp_path):
    from kosherd import mitmca

    ca = tmp_path / "ca.pem"
    anchor = tmp_path / "anchor.crt"
    ca.write_bytes(b"same")
    anchor.write_bytes(b"same")
    monkeypatch.setattr(mitmca, "CA_PEM", ca)
    monkeypatch.setattr(mitmca, "ANCHOR", anchor)
    users = [UserPolicy(uid=1001, username="a", mode="filtered")]
    status = _daemon(users, _all("active\n"), monkeypatch)
    assert status["inspection_ca"] == "ok"
    # The list checks report separately; this is about the certificate.
    assert not any("certificate" in p for p in status["problems"])


def test_the_certificate_is_not_checked_when_nobody_is_inspected(monkeypatch):
    users = [UserPolicy(uid=1001, username="a", mode="dnsfilter")]
    status = _daemon(users, _all("active\n"), monkeypatch)
    assert status["inspection_ca"] == "not needed"
    assert not any("certificate" in p for p in status["problems"])
