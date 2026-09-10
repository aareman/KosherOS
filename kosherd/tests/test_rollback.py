"""Going back, and the machine that puts itself back.

A KosherOS machine has no sudo and no root shell, so a family cannot
repair a bad update from the inside. That makes the rollback path a safety
floor rather than a convenience: if an image boots without the filter
enforcing, the machine has to undo it with nobody present.
"""

import json
from pathlib import Path

import pytest

from kosherd import access
from kosherd.access import ACTIONS, CHANGES, GUARDIAN_GATED
from kosherd.daemon import Daemon, PolicyError
from kosherd.policy import Policy

CHECK = (Path(__file__).parents[2]
         / "os-image/files/etc/greenboot/check/required.d/10-kosher-filter.sh")

# Abridged from `bootc status --json` on a real F44 host: enough shape to
# exercise the reader, including the nesting that has moved between
# releases (status.booted.image.image.image).
STATUS = {
    "status": {
        "booted": {"image": {
            "image": {"image": "ghcr.io/example/kosher-linux:stable"},
            "version": "44.20260910.0", "timestamp": "2026-09-10T00:00:00Z"}},
        "rollback": {"image": {
            "image": {"image": "ghcr.io/example/kosher-linux:stable"},
            "version": "44.20260903.0", "timestamp": "2026-09-03T00:00:00Z"}},
        "staged": None,
        "rollbackQueued": False,
    }
}


def _daemon(monkeypatch, stdout="", returncode=0, stderr="", record=None):
    def run(argv, **kw):
        if record is not None:
            record.append(argv)
        return type("R", (), {"stdout": stdout, "stderr": stderr,
                              "returncode": returncode})()

    monkeypatch.setattr("kosherd.daemon.subprocess.run", run)
    daemon = Daemon.__new__(Daemon)
    daemon.policy = Policy(revision=1)
    return daemon


# ---- the gate ---------------------------------------------------------------

def test_rollback_needs_the_update_right_and_not_the_guardian():
    # greenboot has to do the same thing with no password at all when an
    # image fails its health check, so gating the manual path on the
    # guardian would buy little and risk a machine nobody present can fix.
    assert ACTIONS["Rollback"] == access.ACTION_APPLY_UPDATES
    assert "Rollback" not in GUARDIAN_GATED


def test_going_backwards_is_logged_even_though_going_forwards_is_not():
    # A rollback can restore an older filter, so "who put this machine
    # back?" is a question a parent may need answered.
    assert "Rollback" in CHANGES
    assert "ApplyUpdate" not in CHANGES


def test_reading_the_deployment_is_not_an_admin_write():
    assert ACTIONS["DeploymentStatus"] == access.ACTION_READ_CONFIG


# ---- reading the status -----------------------------------------------------

def test_the_booted_and_rollback_versions_are_both_reported(monkeypatch):
    daemon = _daemon(monkeypatch, stdout=json.dumps(STATUS))
    status = json.loads(daemon.impl_DeploymentStatus().unpack()[0])
    assert status["booted"]["version"] == "44.20260910.0"
    assert status["rollback"]["version"] == "44.20260903.0"
    assert status["booted"]["image"].endswith("kosher-linux:stable")
    assert status["rollback_queued"] is False


def test_a_machine_with_nothing_to_go_back_to_says_so(monkeypatch):
    doc = {"status": {"booted": STATUS["status"]["booted"], "rollback": None}}
    daemon = _daemon(monkeypatch, stdout=json.dumps(doc))
    status = json.loads(daemon.impl_DeploymentStatus().unpack()[0])
    assert status["booted"] is not None
    assert status["rollback"] is None


@pytest.mark.parametrize("stdout,returncode", [
    ("not json at all", 0),
    ("{}", 0),
    ('{"status": {"booted": "a string, not an object"}}', 0),
    ("", 1),
])
def test_unreadable_status_degrades_instead_of_raising(monkeypatch, stdout,
                                                       returncode):
    # The one moment somebody needs this screen is when the machine is
    # already unwell, so it must never be the thing that breaks.
    daemon = _daemon(monkeypatch, stdout=stdout, returncode=returncode,
                     stderr="bootc exploded")
    status = json.loads(daemon.impl_DeploymentStatus().unpack()[0])
    assert "booted" in status


# ---- performing it ----------------------------------------------------------

def test_rollback_calls_bootc_rollback(monkeypatch):
    seen: list = []
    daemon = _daemon(monkeypatch, record=seen)
    assert daemon.impl_Rollback() is None
    assert seen == [["bootc", "rollback"]]


def test_a_failed_rollback_is_reported_not_swallowed(monkeypatch):
    daemon = _daemon(monkeypatch, returncode=1, stderr="no rollback target")
    with pytest.raises(PolicyError, match="no rollback target"):
        daemon.impl_Rollback()


# ---- the greenboot health check ---------------------------------------------

def test_the_health_check_is_shipped_and_is_a_required_one():
    # wanted.d checks only log; required.d is what actually rolls back.
    assert CHECK.exists()
    assert CHECK.parent.name == "required.d"


def test_the_health_check_asserts_the_filter_and_not_the_internet():
    # A family's router being off must never roll the operating system
    # back. Every assertion has to be about this machine.
    body = CHECK.read_text()
    for local in ("kosherd.service", "kosher-dns.service",
                  "nft list table inet kosher"):
        assert local in body, f"the health check no longer checks {local}"
    lowered = body.lower()
    for reaching_out in ("example.com", "1.1.1.1", "8.8.8.8", "curl http",
                         "ping "):
        assert reaching_out not in lowered, \
            f"the health check reaches the network ({reaching_out}); an ISP " \
            "outage would roll the OS back"


def test_the_proxy_is_only_required_where_somebody_is_inspected():
    # Demanding kosher-mitm on a machine where nobody is filtered would
    # roll back a perfectly good image.
    body = CHECK.read_text()
    assert "kosher-mitm.service" in body
    assert "filtered" in body, \
        "the proxy check is no longer conditional on somebody being filtered"
