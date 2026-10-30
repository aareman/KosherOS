"""The VM harness cleans up after itself and never boots a base it cannot
sign in to (scripts/pr-test.py)."""

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
_spec = importlib.util.spec_from_file_location("pr_test", ROOT / "scripts/pr-test.py")
pr_test = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pr_test)

# An ed25519 public key, and its fingerprint as `ssh-keygen -lf` prints it.
PUB = ("ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl "
       "pr-test")


def test_fingerprint_is_what_ssh_keygen_prints(tmp_path):
    if not shutil.which("ssh-keygen"):
        pytest.skip("no ssh-keygen")
    key = tmp_path / "k"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)], check=True)
    out = subprocess.run(["ssh-keygen", "-lf", str(key) + ".pub"], capture_output=True,
                         text=True, check=True).stdout.split()[1]
    assert pr_test.key_fingerprint((tmp_path / "k.pub").read_text()) == out


def test_fingerprint_ignores_the_comment():
    assert pr_test.key_fingerprint(PUB) == pr_test.key_fingerprint(PUB.rsplit(" ", 1)[0])
    assert pr_test.key_fingerprint(PUB).startswith("SHA256:")


def test_a_base_with_the_same_key_is_fine():
    fp = pr_test.key_fingerprint(PUB)
    assert pr_test.base_key_problem({"ssh_key": fp}, fp) == ""


def test_a_base_with_another_key_is_named_as_such():
    fp = pr_test.key_fingerprint(PUB)
    why = pr_test.base_key_problem({"ssh_key": "SHA256:other"}, fp)
    assert "another ssh key" in why and "SHA256:other" in why and fp in why


def test_a_base_with_no_recorded_key_is_not_trusted():
    assert "not recorded" in pr_test.base_key_problem({}, "SHA256:x")


def _base(cache, name, meta):
    d = cache / name
    d.mkdir(parents=True)
    (d / "setup.qcow2").write_text("")
    (d / "meta.json").write_text(json.dumps(meta))
    return d


def test_find_base_skips_a_base_made_with_another_key(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(pr_test, "CACHE", tmp_path)
    fp = pr_test.key_fingerprint(PUB)
    import time
    _base(tmp_path, "base-aaa", {"master": "a" * 40, "created": time.time(),
                                 "ssh_key": "SHA256:other"})
    base, _why = pr_test.find_base("a" * 40, fp)
    assert base is None
    assert "another ssh key" in capsys.readouterr().out


def test_find_base_takes_a_base_made_with_this_key(tmp_path, monkeypatch):
    monkeypatch.setattr(pr_test, "CACHE", tmp_path)
    fp = pr_test.key_fingerprint(PUB)
    import time
    d = _base(tmp_path, "base-bbb", {"master": "b" * 40, "created": time.time(),
                                     "ssh_key": fp})
    _base(tmp_path, "base-ccc", {"master": "c" * 40, "created": time.time() + 10,
                                 "ssh_key": "SHA256:other"})
    base, why = pr_test.find_base("c" * 40, fp)
    assert base == d and "bbbbbbbbb" in why


# --- cleanup ---------------------------------------------------------------------

def _temp(root, name, size=1024):
    p = root / name
    with p.open("wb") as fh:
        fh.truncate(size)
    return p


def test_only_temp_disks_made_while_a_disk_was_made_are_the_runs(tmp_path):
    left = pr_test.Leftovers(tmp_root=tmp_path)
    old = _temp(tmp_path, ".tmpOLD123")
    with left.making_disk():
        mine = _temp(tmp_path, ".tmpNEW456")
        _temp(tmp_path, "not-bcvk")
    later = _temp(tmp_path, ".tmpLAT789")  # after the disk was made: not ours
    assert left.stray_temps() == [mine]
    assert old.exists() and later.exists()


def test_a_run_stopped_while_making_a_disk_counts_up_to_the_stop(tmp_path):
    left = pr_test.Leftovers(tmp_root=tmp_path)
    cm = left.making_disk()
    cm.__enter__()  # stopped half way: the window never closes
    mine = _temp(tmp_path, ".tmpHALF01")
    assert left.stray_temps() == [mine]


def test_a_temp_disk_bcvk_removed_itself_is_not_listed(tmp_path):
    left = pr_test.Leftovers(tmp_root=tmp_path)
    with left.making_disk():
        t = _temp(tmp_path, ".tmpGONE01")
        t.unlink()
    assert left.stray_temps() == []


def _quiet_podman(monkeypatch):
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
    monkeypatch.setattr(pr_test.subprocess, "run", fake_run)
    monkeypatch.setattr(pr_test, "image_id", lambda ref: "")
    return calls


def test_clean_removes_what_the_run_recorded_and_its_temp_disks(tmp_path, monkeypatch):
    _quiet_podman(monkeypatch)
    left = pr_test.Leftovers(tmp_root=tmp_path)
    keep = _temp(tmp_path, ".tmpKEEP01")
    with left.making_disk():
        mine = _temp(tmp_path, ".tmpMINE01")
    machine = left.path(tmp_path / "machine.qcow2")
    machine.write_text("")
    disk_dir = left.path(tmp_path / "disk")
    disk_dir.mkdir()
    (disk_dir / "disk.qcow2").write_text("")
    removed = left.clean()
    assert not mine.exists() and not machine.exists() and not disk_dir.exists()
    assert keep.exists()
    assert str(mine) in removed
    assert left.clean() == []  # once


def test_clean_leaves_a_temp_disk_someone_still_holds_open(tmp_path, monkeypatch):
    _quiet_podman(monkeypatch)
    left = pr_test.Leftovers(tmp_root=tmp_path)
    with left.making_disk():
        held = _temp(tmp_path, ".tmpHELD01")
    with held.open("rb"):
        left.clean()
        assert held.exists()


def test_keep_keeps_everything(tmp_path, monkeypatch):
    calls = _quiet_podman(monkeypatch)
    left = pr_test.Leftovers(tmp_root=tmp_path)
    left.keep = True
    with left.making_disk():
        mine = _temp(tmp_path, ".tmpKEPT01")
    machine = left.path(tmp_path / "machine.qcow2")
    machine.write_text("")
    left.image("localhost/kosher-linux:pr-test-x")
    assert left.clean() == []
    assert mine.exists() and machine.exists() and calls == []


def test_clean_folds_the_run_image_into_the_build_cache(tmp_path, monkeypatch):
    _quiet_podman(monkeypatch)
    monkeypatch.setattr(pr_test, "image_id", lambda ref: "abc")
    folded = []
    monkeypatch.setattr(pr_test, "keep_build_cache", folded.append)
    left = pr_test.Leftovers(tmp_root=tmp_path)
    left.image("localhost/kosher-linux:pr-test-x")
    left.clean()
    assert folded == ["localhost/kosher-linux:pr-test-x"]
    assert pr_test.CACHE_TAG == "localhost/kosher-linux:pr-test-cache"


def test_clean_stops_the_processes_the_run_started(tmp_path, monkeypatch):
    left = pr_test.Leftovers(tmp_root=tmp_path)
    proc = left.start(["sleep", "300"])
    _quiet_podman(monkeypatch)
    left.clean()
    assert proc.poll() is not None


def test_prune_takes_old_unused_dangling_images_only():
    now = 1_000_000.0
    images = [{"id": "old", "added": now - 2 * 86400},
              {"id": "new", "added": now - 3600},
              {"id": "used", "added": now - 5 * 86400},
              {"id": "unknown", "added": None}]
    assert pr_test.dangling_to_prune(images, {"used"}, now) == ["old"]


def test_no_session_says_the_runner_has_none_in_ci():
    msg = pr_test.no_session_reason(True, "Gdm: GdmDisplay: Session never registered, failing",
                                    ci=True)
    assert "runner has no working graphical session" in msg
    assert "runner" not in pr_test.no_session_reason(True, "", ci=True)
    assert "did not stay up" in pr_test.no_session_reason(False, "", ci=False)


def test_the_label_reaches_bcvk():
    text = (ROOT / "scripts/rootless-disk.sh").read_text()
    assert "KOSHER_DISK_LABEL" in text and '"${label[@]}"' in text
    assert pr_test.Leftovers().label.startswith("kosher.pr-test.run=")


def test_the_cache_tag_is_never_a_run_image():
    assert os.path.basename(pr_test.CACHE_TAG) == "kosher-linux:pr-test-cache"


def test_the_suite_shards_run_every_suite_once():
    # CI splits a run over runners (vm-test.yml): each works out the same
    # groups from the same selection and takes its own.
    suites = sorted(pr_test.SUITE_SECONDS)
    for n in (1, 2, 3, 4):
        parts = [pr_test.shard_groups(suites, pr_test.parse_shard(f"suites:{k}/{n}")[1])
                 for k in range(1, n + 1)]
        ran = [s for part in parts for group in part for s in group]
        assert sorted(ran) == suites, n
        assert all(len(part) == 1 for part in parts)


def test_a_shard_past_the_suites_runs_nothing():
    assert pr_test.shard_groups(["10-core"], (2, 3)) == []


def test_the_desktop_shard_runs_no_suites():
    desktop, part = pr_test.parse_shard("desktop")
    assert desktop and pr_test.shard_groups(sorted(pr_test.SUITE_SECONDS), part) == []
    assert pr_test.parse_shard("all") == (True, None)
    assert pr_test.parse_shard("suites:2/3") == (False, (2, 3))
    with pytest.raises(SystemExit):
        pr_test.parse_shard("suites:4/3")


def test_a_registry_image_is_a_name_pinned_to_its_digest():
    d = "sha256:" + "0" * 64
    assert pr_test.parse_registry_image(f"ghcr.io/x/kosher-linux-ci:pr-7-1@{d}") == \
        ("ghcr.io/x/kosher-linux-ci", d)
    assert pr_test.parse_registry_image(f"ghcr.io/x/kosher-linux@{d}") == ("ghcr.io/x/kosher-linux", d)
    for bad in ("ghcr.io/x/kosher-linux:edge", f"ghcr.io/x/kosher-linux@{d[:-1]}", ""):
        with pytest.raises(SystemExit):
            pr_test.parse_registry_image(bad)


def test_a_switch_to_a_public_registry_pins_the_digest_and_leaves_no_trust_behind():
    # The harness's own registry is added to the machine's trust for the
    # switch and taken out again; a public one the policy already accepts
    # gets neither, and the switch is to the digest (pr-test.py).
    src = (ROOT / "scripts/pr-test.py").read_text()
    assert 'target = ref if local else f"{ref}@{digest}"' in src
    assert '(SWITCH_SETUP if local else "set -e\\n")' in src
    assert "if local:\n            rc, out = vm.sh(CLEAN_SWITCH)" in src


def test_a_switch_by_name_waits_for_the_machines_resolver():
    # kosher-dns is up a few seconds after sshd; a switch to ghcr before
    # it answered failed with "Temporary failure in name resolution".
    src = (ROOT / "scripts/pr-test.py").read_text()
    assert "getent hosts {host}" in src and 'host = ref.split("/", 1)[0]' in src


def _c(name, ok, detail="", **more):
    return {"name": name, "ok": ok, "detail": detail, **more}


def test_a_check_that_fails_both_runs_is_a_failure_and_once_is_flaky():
    first = [_c("s: a", True), _c("s: b", False, "boom"), _c("s: c", False, "twice")]
    second = [_c("s: a", True), _c("s: b", True), _c("s: c", False, "twice again")]
    merged = pr_test.merge_runs(first, second)
    assert [c["name"] for c in merged] == ["s: a", "s: b", "s: c"]
    assert merged[0] == _c("s: a", True)
    assert merged[1]["ok"] and merged[1]["flaky"]
    assert merged[1]["detail"] == "failed on the first of two runs: boom"
    assert not merged[2]["ok"] and "flaky" not in merged[2]
    assert merged[2]["detail"] == "twice again", "the second run's words"


def test_a_check_that_passes_first_and_fails_second_is_flaky_too():
    merged = pr_test.merge_runs([_c("s: a", True)], [_c("s: a", False, "later")])
    assert merged[0]["ok"] and merged[0]["flaky"]
    assert merged[0]["detail"] == "failed on the second of two runs: later"


def test_a_check_only_one_run_reached_stands_as_it_was():
    # The first run stopped early (run.sh died); the second got further.
    first = [_c("s: a", True), _c("s: run.sh finished", False, "exit 1")]
    second = [_c("s: a", True), _c("s: b", True), _c("s: c", True)]
    merged = pr_test.merge_runs(first, second)
    names = [c["name"] for c in merged]
    assert names == ["s: a", "s: run.sh finished", "s: b", "s: c"]
    assert not merged[1]["ok"], "the early death is not forgotten"


def test_the_verdict_lists_flaky_checks_and_the_report_shows_them(tmp_path):
    stages = [{"name": "suites-1", "status": "passed", "seconds": 1, "note": "", "log": "x",
               "checks": [_c("90-developer: it runs a rootless container", True,
                             "failed on the first of two runs: unable to copy", flaky=True),
                          _c("10-core: boots", True)]}]
    verdict = pr_test.classify(stages, [])
    assert verdict["new"] == [] and verdict["known"] == []
    assert [f["check"] for f in verdict["flaky"]] == ["90-developer: it runs a rootless container"]
    summary = {"target": "HEAD", "commit": "abc1234567", "result": "passed", "seconds": 60,
               "mode": "fast", "stages": stages, "failures": verdict}
    pr_test.write_report(tmp_path, summary)
    report = (tmp_path / "report.md").read_text()
    assert "Flaky: 1." in report
    assert "## Passed on a second run" in report
    assert "unable to copy" in report


def test_a_second_run_can_be_switched_off():
    src = (ROOT / "scripts/pr-test.py").read_text()
    assert '"--no-retry"' in src
    assert "retry=not args.no_retry" in src
    assert 'if args.no_retry or all(c["ok"] for c in st.checks):' in src
