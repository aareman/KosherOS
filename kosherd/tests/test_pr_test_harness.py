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
