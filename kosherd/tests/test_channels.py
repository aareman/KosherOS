"""Moving a machine between update channels.

Two channels exist — stable, which families run, and edge, which gets
every build the day it is made — and until now a machine was stuck on
whichever one its installer ISO was built from. Getting off it meant a
root shell, which a KosherOS machine does not have.

The switch is `bootc switch` onto the other tag of the same repository:
the same pull as an update, staged for the next restart, with nothing on
the machine touched.
"""

import json

import pytest

from kosherd import access, updates
from kosherd.access import ACTIONS, CHANGES, GUARDIAN_GATED
from kosherd.daemon import Daemon, PolicyError
from kosherd.policy import Policy


def _status(booted="ghcr.io/example/kosher-linux:stable", staged=None):
    def entry(ref):
        return {"image": {"image": {"image": ref}, "version": "0.2.0",
                          "timestamp": "2026-09-10T00:00:00Z"}}

    return {"status": {"booted": entry(booted),
                       "staged": entry(staged) if staged else None,
                       "rollback": None, "rollbackQueued": False}}


def _daemon(monkeypatch, stdout="", returncode=0, stderr=""):
    monkeypatch.setattr("kosherd.daemon.subprocess.run",
                        lambda argv, **kw: type("R", (), {
                            "stdout": stdout, "stderr": stderr,
                            "returncode": returncode})())
    daemon = Daemon.__new__(Daemon)
    daemon.policy = Policy(revision=1)
    return daemon


# ---- naming the other image -------------------------------------------------

@pytest.mark.parametrize("image,expected", [
    ("ghcr.io/aareman/kosher-linux:stable", "ghcr.io/aareman/kosher-linux"),
    ("ghcr.io/aareman/kosher-linux:v0.2.0", "ghcr.io/aareman/kosher-linux"),
    ("ghcr.io/aareman/kosher-linux@sha256:abc", "ghcr.io/aareman/kosher-linux"),
    ("ghcr.io/aareman/kosher-linux", "ghcr.io/aareman/kosher-linux"),
    # The colon in a registry's port is not a tag.
    ("localhost:5000/kosher-linux:edge", "localhost:5000/kosher-linux"),
    ("kosher-linux:dev", "kosher-linux"),
])
def test_the_repository_is_read_without_the_tag_or_digest(image, expected):
    assert updates.repo_of(image) == expected


def test_switching_keeps_the_repository_the_machine_already_runs():
    # A fork, a local registry or a test machine switches within its own
    # repository rather than being quietly moved onto ghcr.io.
    assert updates.switch_target("registry.example/fork/kosher-linux:stable",
                                 "edge") == "registry.example/fork/kosher-linux:edge"


def test_a_machine_pinned_to_one_version_can_rejoin_a_channel():
    # This is how a machine parked on a release starts updating again.
    assert updates.switch_target("ghcr.io/x/kosher-linux:v0.2.0", "stable") == \
        "ghcr.io/x/kosher-linux:stable"


def test_an_unknown_channel_is_refused_rather_than_pulled():
    with pytest.raises(ValueError, match="unknown channel"):
        updates.switch_target("ghcr.io/x/kosher-linux:edge", "nightly")


def test_a_machine_that_does_not_say_what_it_runs_cannot_switch():
    with pytest.raises(ValueError, match="nothing to switch from"):
        updates.switch_target("", "stable")


# ---- describing them --------------------------------------------------------

def test_both_channels_are_listed_with_the_running_one_marked():
    info = updates.describe_channels("ghcr.io/x/kosher-linux:stable")
    assert info["current"] == "stable"
    assert [c["name"] for c in info["channels"]] == ["stable", "edge"]
    assert [c["current"] for c in info["channels"]] == [True, False]
    # Every channel says what it means, because the person choosing is a
    # parent and not somebody who knows what a container tag is.
    assert all(c["summary"] and c["description"] for c in info["channels"])


def test_the_default_is_stable():
    # Edge is the maintainer's channel; a family computer wants stable.
    assert updates.DEFAULT_CHANNEL == "stable"
    assert updates.CHANNEL_NAMES == ("stable", "edge")


def test_a_machine_on_no_channel_at_all_is_a_state_and_not_an_error():
    # Every machine installed before channels existed is in this state, as
    # is anything running a pinned version or a locally built image.
    for image in ("ghcr.io/x/kosher-linux:v0.2.0",
                  "ghcr.io/x/kosher-linux@sha256:abc",
                  "localhost/kosher-linux:dev", ""):
        info = updates.describe_channels(image)
        assert info["current"] is None
        assert len(info["channels"]) == 2


def test_a_switch_already_staged_is_reported_as_pending():
    # A switch does not move the running deployment, only the one queued
    # for the next restart; without this the screen would say the switch
    # had not happened.
    info = updates.describe_channels("ghcr.io/x/kosher-linux:stable",
                                     "ghcr.io/x/kosher-linux:edge")
    assert info["current"] == "stable" and info["pending"] == "edge"
    assert [c["pending"] for c in info["channels"]] == [False, True]


def test_an_ordinary_staged_update_is_not_a_pending_switch():
    info = updates.describe_channels("ghcr.io/x/kosher-linux:edge",
                                     "ghcr.io/x/kosher-linux:edge")
    assert info["pending"] is None


# ---- the gate ---------------------------------------------------------------

def test_switching_channels_needs_the_update_right_and_the_guardian():
    # Moving the family computer onto edge puts it on builds nobody has
    # tried yet — the kind of decision the second password exists for.
    assert ACTIONS["SetChannel"] == access.ACTION_APPLY_UPDATES
    assert "SetChannel" in GUARDIAN_GATED
    assert ACTIONS["ListChannels"] == access.ACTION_READ_CONFIG
    assert "ListChannels" not in GUARDIAN_GATED


def test_the_switch_is_written_to_the_activity_log():
    assert "SetChannel" in CHANGES
    # Its first argument is a channel name, not a uid, so it must not be
    # logged as being about an account.
    assert "SetChannel" not in access.PER_USER


# ---- the daemon -------------------------------------------------------------

def test_listing_channels_says_which_one_this_machine_follows(monkeypatch):
    daemon = _daemon(monkeypatch, stdout=json.dumps(_status()))
    info = json.loads(daemon.impl_ListChannels().unpack()[0])
    assert info["current"] == "stable"
    assert info["image"] == "ghcr.io/example/kosher-linux:stable"
    assert [c["name"] for c in info["channels"]] == ["stable", "edge"]


def test_listing_channels_sees_a_switch_waiting_for_the_next_restart(monkeypatch):
    daemon = _daemon(monkeypatch, stdout=json.dumps(
        _status(staged="ghcr.io/example/kosher-linux:edge")))
    info = json.loads(daemon.impl_ListChannels().unpack()[0])
    assert info["pending"] == "edge"


def test_switching_runs_bootc_switch_onto_the_other_tag(monkeypatch):
    seen: list = []
    monkeypatch.setattr(updates, "run_switch",
                        lambda image, on_p, on_f: seen.append(image) or "thread")
    daemon = _daemon(monkeypatch, stdout=json.dumps(_status()))
    assert daemon.impl_SetChannel("edge", "") is None
    assert seen == ["ghcr.io/example/kosher-linux:edge"]


def test_switching_to_the_channel_already_in_use_is_refused(monkeypatch):
    daemon = _daemon(monkeypatch, stdout=json.dumps(_status()))
    with pytest.raises(PolicyError, match="already follows the stable channel"):
        daemon.impl_SetChannel("stable", "")


def test_an_unknown_channel_never_reaches_bootc(monkeypatch):
    # Whatever arrives on the bus, only the two names this build knows can
    # become an image reference to pull.
    monkeypatch.setattr(updates, "run_switch", lambda *a: pytest.fail(
        "an unknown channel was pulled"))
    daemon = _daemon(monkeypatch, stdout=json.dumps(_status()))
    with pytest.raises(PolicyError, match="unknown channel"):
        daemon.impl_SetChannel("../../evil:latest", "")


def test_switching_while_an_update_is_running_is_refused(monkeypatch):
    daemon = _daemon(monkeypatch, stdout=json.dumps(_status()))
    daemon._update_thread = type("T", (), {"is_alive": lambda self: True})()
    with pytest.raises(PolicyError, match="already downloading an update"):
        daemon.impl_SetChannel("edge", "")


def test_a_machine_that_cannot_say_what_it_runs_gets_an_explanation(monkeypatch):
    daemon = _daemon(monkeypatch, returncode=1, stderr="bootc exploded")
    with pytest.raises(PolicyError, match="nothing to switch from"):
        daemon.impl_SetChannel("edge", "")


def test_a_switch_reports_on_the_same_signals_as_an_update(monkeypatch):
    # To the person watching it is the same wait, so it uses the same
    # progress bar rather than a second one of its own.
    seen: list = []
    monkeypatch.setattr(updates, "run_switch",
                        lambda image, on_p, on_f: seen.append((on_p, on_f)))
    daemon = _daemon(monkeypatch, stdout=json.dumps(_status()))
    daemon.impl_SetChannel("edge", "")
    assert seen[0] == (daemon._on_update_progress, daemon._on_update_finished)
