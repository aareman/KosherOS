"""Trying a build that is on no channel, keeping the one the machine came
from (#90).

The maintainer's laptop runs edge. A pull request's image is a tag on the
same repository; switching onto it is an ordinary `bootc switch`, and the
second switch would drop the edge deployment, because bootc keeps the
booted deployment and the new one and nothing else. So the first try pins
what is running, every try removes the test builds no longer booted, and
"done" rolls back onto the pinned one and unpins it. All of the deciding is
in pure functions over bootc's status; the daemon only carries it out.
"""

from __future__ import annotations

import json

import pytest

from kosherd import access, updates
from kosherd.access import ACTIONS, CHANGES, GUARDIAN_GATED
from kosherd.daemon import Daemon, PolicyError
from kosherd.policy import Policy

REPO = "ghcr.io/aareman/kosher-linux"
EDGE = f"{REPO}:edge"
PR7 = f"{REPO}:pr-7"
PR8 = f"{REPO}:pr-8"
NO_POLICY = "/nonexistent/policy.json"   # no signature required: plain bootc commands


def _key(letter: str, serial: int = 0) -> str:
    return f"{letter * 64}.{serial}"


def _entry(ref, key=_key("a"), pinned=False, verified=True):
    checksum, serial = key.rsplit(".", 1)
    return {"image": {"image": {"image": ref, "transport": "registry",
                                "signature": "containerPolicy" if verified else None},
                      "version": "0.15.0", "imageDigest": "sha256:" + checksum[:12]},
            "pinned": pinned,
            "ostree": {"stateroot": "default", "checksum": checksum,
                       "deploySerial": int(serial)}}


def _status(booted, staged=None, rollback=None, others=()):
    return {"booted": booted, "staged": staged, "rollback": rollback,
            "rollbackQueued": False, "otherDeployments": list(others)}


def _deps(**kw):
    return updates.deployments(_status(**kw))


# ---- naming the build -------------------------------------------------------

@pytest.mark.parametrize("what,expected", [
    ("7", PR7),
    ("pr-7", PR7),
    ("007", PR7),
    ("v0.14.0", f"{REPO}:v0.14.0"),
    ("2049571ab3cd", f"{REPO}:2049571ab3cd"),
    (f"{REPO}:pr-9", f"{REPO}:pr-9"),
])
def test_a_number_a_tag_or_a_reference_of_this_repository_name_a_build(what, expected):
    assert updates.trial_target(EDGE, what) == expected


@pytest.mark.parametrize("what,why", [
    ("", "which build"),
    ("ghcr.io/somebody/else:pr-7", "not a build of this computer's image"),
    ("edge", "is a channel"),
    ("Stable", "is a channel"),
    ("pr 7", "not a tag"),
    ("-bad", "not a tag"),
])
def test_other_repositories_channels_and_nonsense_are_refused(what, why):
    with pytest.raises(ValueError, match=why):
        updates.trial_target(EDGE, what)


def test_a_machine_that_does_not_say_what_it_runs_cannot_try_anything():
    with pytest.raises(ValueError, match="does not say"):
        updates.trial_target("", "7")


# ---- reading what is on the disk --------------------------------------------

def test_every_deployment_bootc_lists_is_read_with_its_ostree_key():
    deps = _deps(booted=_entry(PR7, _key("b")), staged=_entry(PR8, _key("c")),
                 rollback=_entry(EDGE, _key("a"), pinned=True),
                 others=[_entry(f"{REPO}:pr-5", _key("d", 1))])
    assert [(d["place"], d["image"]) for d in deps] == [
        ("staged", PR8), ("booted", PR7), ("rollback", EDGE), ("other", f"{REPO}:pr-5")]
    assert [d["key"] for d in deps] == [_key("c"), _key("b"), _key("a"), _key("d", 1)]
    assert [d["pinned"] for d in deps] == [False, False, True, False]
    assert [d["channel"] for d in deps] == [None, None, "edge", None]


def test_an_older_bootc_without_the_ostree_block_or_other_deployments_still_reads():
    entry = {"image": {"image": {"image": EDGE}, "version": "0.1"}}
    deps = updates.deployments({"booted": entry, "staged": None, "rollback": None})
    assert deps == [{"place": "booted", "image": EDGE, "channel": "edge", "digest": None,
                     "pinned": False, "verified": False, "key": None}]


OSTREE_STATUS = f"""\
  default {'c' * 64}.0 (staged)
    Version: 0.15.0-pr.8+g1234567
    origin: ostree-image-signed:docker://{PR8}
* default {'b' * 64}.0
    Version: 0.15.0-pr.7+gabcdef0
    origin: ostree-image-signed:docker://{PR7}
  default {'a' * 64}.1 (rollback) (pinned)
    Version: 0.14.2
    origin: ostree-image-signed:docker://{EDGE}
"""


def test_ostrees_status_gives_each_deployment_its_index():
    rows = updates.parse_ostree_status(OSTREE_STATUS)
    assert [(r["index"], r["key"]) for r in rows] == [
        (0, _key("c")), (1, _key("b")), (2, _key("a", 1))]
    assert [r["booted"] for r in rows] == [False, True, False]
    assert [r["pinned"] for r in rows] == [False, False, True]
    assert [r["staged"] for r in rows] == [True, False, False]
    assert updates.parse_ostree_status("") == []
    assert updates.parse_ostree_status("No deployments.") == []


# ---- the first try, and the ones after ----------------------------------------

def test_the_first_try_pins_what_is_running_and_switches():
    deps = _deps(booted=_entry(EDGE, _key("a")), rollback=_entry(EDGE, _key("z")))
    plan = updates.trial_plan(deps, PR7, NO_POLICY)
    assert plan["pin"] == _key("a")
    assert plan["kept"]["image"] == EDGE
    assert plan["argv"] == ("bootc", "switch", PR7)
    # The older edge behind it is a channel deployment: ostree's own
    # business, not a test build to remove.
    assert plan["undeploy"] == []


def test_the_switch_is_verified_where_the_policy_demands_it(tmp_path):
    policy = tmp_path / "policy.json"
    policy.write_text(json.dumps({"default": [{"type": "reject"}], "transports": {"docker": {
        REPO: [{"type": "sigstoreSigned", "keyPath": "/k.pub"}]}}}))
    plan = updates.trial_plan(_deps(booted=_entry(EDGE, _key("a"))), PR7, str(policy))
    assert plan["argv"] == ("bootc", "switch", "--enforce-container-sigpolicy", PR7)


def test_the_second_try_pins_nothing_and_removes_the_build_tried_before():
    # Booted into pr-7, pr-5 behind it from the try before, edge pinned.
    deps = _deps(booted=_entry(PR7, _key("b")), rollback=_entry(f"{REPO}:pr-5", _key("d")),
                 others=[_entry(EDGE, _key("a"), pinned=True)])
    plan = updates.trial_plan(deps, PR8, NO_POLICY)
    assert plan["pin"] is None
    assert plan["kept"]["image"] == EDGE
    assert plan["undeploy"] == [_key("d")]
    assert plan["argv"] == ("bootc", "switch", PR8)


def test_trying_the_build_already_running_fetches_its_newest_push():
    # `bootc switch` onto the reference in use is a no-op; upgrade pulls
    # the newest digest of it, which after a new push is the point.
    plan = updates.trial_plan(_deps(booted=_entry(PR7, _key("b")),
                                    others=[_entry(EDGE, _key("a"), pinned=True)]),
                              PR7, NO_POLICY)
    assert plan["argv"] == ("bootc", "upgrade")


def test_trying_the_build_already_staged_fetches_its_newest_push_too(tmp_path):
    policy = tmp_path / "policy.json"
    policy.write_text(json.dumps({"default": [{"type": "reject"}], "transports": {"docker": {
        REPO: [{"type": "sigstoreSigned", "keyPath": "/k.pub"}]}}}))
    # Booted edge never verified (a fresh install), pr-7 staged. The
    # first-verified-update switch must not fire here: it would stage
    # edge again and throw the staged build away.
    deps = _deps(booted=_entry(EDGE, _key("a"), pinned=True, verified=False),
                 staged=_entry(PR7, _key("b")))
    plan = updates.trial_plan(deps, PR7, str(policy))
    assert plan["argv"] == ("bootc", "upgrade")
    assert plan["undeploy"] == []


def test_a_machine_with_no_booted_image_cannot_keep_it():
    with pytest.raises(ValueError, match="does not say"):
        updates.trial_plan([], PR7, NO_POLICY)
    booted = updates._deployment("booted", {"image": {"image": {"image": EDGE}}})
    with pytest.raises(ValueError, match="cannot be kept"):
        updates.trial_plan([booted], PR7, NO_POLICY)


# ---- done ---------------------------------------------------------------------

def test_done_removes_the_builds_in_between_and_rolls_back_onto_the_kept_one():
    deps = _deps(booted=_entry(PR8, _key("c")), rollback=_entry(PR7, _key("b")),
                 others=[_entry(EDGE, _key("a"), pinned=True)])
    plan = updates.return_plan(deps)
    assert plan["kept"]["image"] == EDGE
    assert plan["undeploy"] == [_key("b")]
    assert plan["rollback"] is True
    assert plan["unpin"] == _key("a")


def test_done_with_a_build_only_staged_drops_it_and_rolls_nothing_back():
    deps = _deps(booted=_entry(EDGE, _key("a"), pinned=True), staged=_entry(PR7, _key("b")))
    plan = updates.return_plan(deps)
    assert plan["rollback"] is False
    assert plan["undeploy"] == [_key("b")]
    assert plan["unpin"] == _key("a")


def test_done_leaves_a_staged_build_to_bootc_rollback_which_discards_it():
    deps = _deps(booted=_entry(PR7, _key("b")), staged=_entry(PR8, _key("c")),
                 rollback=_entry(EDGE, _key("a"), pinned=True))
    plan = updates.return_plan(deps)
    assert plan["rollback"] is True
    assert plan["undeploy"] == []


def test_done_when_nothing_is_kept_is_refused_in_words():
    with pytest.raises(ValueError, match="not trying a build"):
        updates.return_plan(_deps(booted=_entry(EDGE)))


# ---- the gate -----------------------------------------------------------------

def test_trying_a_build_needs_the_update_right_and_the_guardian():
    assert ACTIONS["TryImage"] == access.ACTION_APPLY_UPDATES
    assert "TryImage" in GUARDIAN_GATED, "a build nobody has tested, like a switch to edge"


def test_going_back_to_the_kept_build_needs_the_update_right_and_not_the_guardian():
    assert ACTIONS["ReturnToKept"] == access.ACTION_APPLY_UPDATES
    assert "ReturnToKept" not in GUARDIAN_GATED, "an image this machine already ran"


def test_both_are_written_to_the_activity_log_and_answered_off_the_main_loop():
    from kosherd import daemon as daemon_mod

    assert {"TryImage", "ReturnToKept"} <= CHANGES
    assert {"TryImage", "ReturnToKept"} <= daemon_mod.IN_BACKGROUND


# ---- the daemon carrying it out -----------------------------------------------

class _Machine:
    """A pretend bootc and ostree: answers status from `docs` (one per
    bootc status call, the last one repeating), lists ostree's
    deployments from `ostree`, and records every command."""

    def __init__(self, docs, ostree):
        self.docs, self.ostree, self.calls = list(docs), ostree, []

    def __call__(self, argv, **kw):
        self.calls.append(list(argv))
        out = ""
        if argv[:2] == ["bootc", "status"]:
            out = json.dumps({"status": self.docs.pop(0) if len(self.docs) > 1 else self.docs[0]})
        elif argv[:3] == ["ostree", "admin", "status"]:
            out = self.ostree
        elif argv[:2] == ["systemctl", "is-active"]:
            out = "inactive"
        return type("R", (), {"stdout": out, "stderr": "", "returncode": 0})()


def _daemon(monkeypatch, machine, started):
    monkeypatch.setattr("kosherd.daemon.subprocess.run", machine)
    monkeypatch.setattr(updates, "run_upgrade",
                        lambda on_progress, on_finished, argv=("bootc", "upgrade"):
                        started.append(tuple(argv)) or type("T", (), {"is_alive": lambda s: False})())
    monkeypatch.setattr(updates, "POLICY_PATH", NO_POLICY)
    daemon = Daemon.__new__(Daemon)
    daemon.policy = Policy(revision=1)
    return daemon


def test_the_first_try_pins_the_booted_deployment_then_starts_the_switch(monkeypatch):
    started: list = []
    machine = _Machine([_status(booted=_entry(EDGE, _key("a")))],
                       f"* default {'a' * 64}.0\n    Version: 0.14.2\n")
    daemon = _daemon(monkeypatch, machine, started)
    assert daemon.impl_TryImage("7", "") is None
    assert ["ostree", "admin", "pin", "0"] in machine.calls
    assert not any(c[:3] == ["ostree", "admin", "undeploy"] for c in machine.calls)
    assert started == [("bootc", "switch", "--enforce-container-sigpolicy", PR7)] \
        or started == [("bootc", "switch", PR7)]


def test_a_later_try_undeploys_the_stale_build_by_its_ostree_index(monkeypatch):
    started: list = []
    machine = _Machine(
        [_status(booted=_entry(PR7, _key("b")), rollback=_entry(f"{REPO}:pr-5", _key("d")),
                 others=[_entry(EDGE, _key("a"), pinned=True)])],
        f"* default {'b' * 64}.0\n  default {'d' * 64}.0 (rollback)\n"
        f"  default {'a' * 64}.0 (pinned)\n")
    daemon = _daemon(monkeypatch, machine, started)
    daemon.impl_TryImage("8", "")
    assert ["ostree", "admin", "undeploy", "1"] in machine.calls
    assert not any(c[:3] == ["ostree", "admin", "pin"] for c in machine.calls)
    assert started and started[0][-1] == PR8


def test_a_build_of_another_repository_never_reaches_bootc_or_ostree(monkeypatch):
    started: list = []
    machine = _Machine([_status(booted=_entry(EDGE, _key("a")))], "")
    daemon = _daemon(monkeypatch, machine, started)
    with pytest.raises(PolicyError, match="not a build of this computer's image"):
        daemon.impl_TryImage("ghcr.io/somebody/else:pr-1", "")
    assert started == []
    assert not any(c[0] == "ostree" for c in machine.calls)


def test_done_undeploys_the_middle_build_rolls_back_and_unpins(monkeypatch):
    before = _status(booted=_entry(PR8, _key("c")), rollback=_entry(PR7, _key("b")),
                     others=[_entry(EDGE, _key("a"), pinned=True)])
    # After the undeploy, edge is next in line.
    after = _status(booted=_entry(PR8, _key("c")), rollback=_entry(EDGE, _key("a"), pinned=True))
    machine = _Machine([before, after],
                       f"* default {'c' * 64}.0\n  default {'b' * 64}.0 (rollback)\n"
                       f"  default {'a' * 64}.0 (pinned)\n")
    daemon = _daemon(monkeypatch, machine, [])
    assert daemon.impl_ReturnToKept() is None
    ostree = [c for c in machine.calls if c[0] == "ostree" and c[2] != "status"]
    assert ostree == [["ostree", "admin", "undeploy", "1"],
                      ["ostree", "admin", "pin", "--unpin", "2"]], ostree
    assert ["bootc", "rollback"] in machine.calls
    assert machine.calls.index(["bootc", "rollback"]) > machine.calls.index(ostree[0])


def test_done_refuses_to_roll_back_onto_anything_but_the_kept_build(monkeypatch):
    before = _status(booted=_entry(PR8, _key("c")), rollback=_entry(PR7, _key("b")),
                     others=[_entry(EDGE, _key("a"), pinned=True)])
    # Pretend the undeploy changed nothing: pr-7 is still next in line.
    machine = _Machine([before, before],
                       f"* default {'c' * 64}.0\n  default {'b' * 64}.0 (rollback)\n"
                       f"  default {'a' * 64}.0 (pinned)\n")
    daemon = _daemon(monkeypatch, machine, [])
    with pytest.raises(PolicyError, match="not the one a restart would go back to"):
        daemon.impl_ReturnToKept()
    assert ["bootc", "rollback"] not in machine.calls
    assert not any(c[:4] == ["ostree", "admin", "pin", "--unpin"] for c in machine.calls)


def test_done_with_nothing_kept_says_so(monkeypatch):
    machine = _Machine([_status(booted=_entry(EDGE, _key("a")))], "")
    daemon = _daemon(monkeypatch, machine, [])
    with pytest.raises(PolicyError, match="not trying a build"):
        daemon.impl_ReturnToKept()


def test_the_status_names_the_kept_deployment_wherever_bootc_lists_it(monkeypatch):
    machine = _Machine([_status(booted=_entry(PR8, _key("c")), rollback=_entry(PR7, _key("b")),
                                others=[_entry(EDGE, _key("a"), pinned=True)])], "")
    daemon = _daemon(monkeypatch, machine, [])
    status = json.loads(daemon.impl_DeploymentStatus().unpack()[0])
    assert status["kept"]["image"] == EDGE and status["kept"]["pinned"] is True
    assert status["booted"]["pinned"] is False


# ---- kosherctl ----------------------------------------------------------------

def test_kosherctl_try_done_with_nothing_kept_stops_before_the_daemon(monkeypatch, capsys):
    from kosherd import cli

    class Client:
        def deployment_status(self):
            return {"booted": {"image": EDGE, "version": "0.14.2"}, "kept": None}

        def return_to_kept(self):
            raise AssertionError("not reached")

    monkeypatch.setattr(cli, "_client", lambda: Client())
    args = type("A", (), {"action": "try", "value": "done", "guardian_password": None})()
    assert cli.cmd_system(args) == 1
    assert "not trying a build" in capsys.readouterr().err


def test_kosherctl_try_sends_the_number_and_the_guardian_password(monkeypatch, capsys):
    from kosherd import cli

    sent = []

    class Client:
        def try_image(self, what, pw=""):
            sent.append((what, pw))

    monkeypatch.setattr(cli, "_client", lambda: Client())
    monkeypatch.setattr(cli, "_guardian_pw", lambda args: "secret")
    args = type("A", (), {"action": "try", "value": "7", "guardian_password": "secret"})()
    assert cli.cmd_system(args) == 0
    assert sent == [("7", "secret")]
    out = capsys.readouterr().out
    assert "try done" in out and "kept" in out
