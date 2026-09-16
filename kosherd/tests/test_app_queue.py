"""The install queue.

Requests used to be refused while another install ran ("app store can only
install one at a time"). flatpak genuinely cannot run two transactions
against one installation, so the fix was a queue — pinned here.
"""

import threading
import time

import pytest

from kosherd.apps import AppError, AppManager


class Recorder:
    def __init__(self):
        self.progress: list[tuple] = []
        self.finished: list[tuple] = []
        self.done = threading.Event()
        self.expected = 1

    def on_progress(self, ref, percent, status):
        self.progress.append((ref, percent, status))

    def on_finished(self, ref, ok, error):
        self.finished.append((ref, ok, error))
        if len(self.finished) >= self.expected:
            self.done.set()

    def wait(self, timeout=5):
        assert self.done.wait(timeout), f"jobs did not finish: {self.finished}"

    @property
    def refs_finished(self):
        return [ref for ref, _ok, _err in self.finished]


@pytest.fixture
def manager(monkeypatch):
    """An AppManager whose flatpak work is replaced by a short sleep."""
    rec = Recorder()
    mgr = AppManager(rec.on_progress, rec.on_finished)
    mgr.recorder = rec
    mgr.started: list[str] = []
    mgr.concurrent = 0
    mgr.max_concurrent = 0

    def work(ref):
        mgr.started.append(ref)
        mgr.concurrent += 1
        mgr.max_concurrent = max(mgr.max_concurrent, mgr.concurrent)
        time.sleep(0.05)
        mgr.concurrent -= 1

    monkeypatch.setattr(mgr, "_do_install", work)
    monkeypatch.setattr(mgr, "_do_remove", work)
    monkeypatch.setattr("kosherd.apps.allowed_refs",
                        lambda: {"org.a.A", "org.b.B", "org.c.C"})
    return mgr


def test_several_installs_are_queued_not_refused(manager):
    manager.recorder.expected = 3
    for ref in ("org.a.A", "org.b.B", "org.c.C"):
        manager.install(ref)  # must not raise
    manager.recorder.wait()
    assert sorted(manager.recorder.refs_finished) == ["org.a.A", "org.b.B", "org.c.C"]


def test_jobs_run_one_at_a_time(manager):
    manager.recorder.expected = 3
    for ref in ("org.a.A", "org.b.B", "org.c.C"):
        manager.install(ref)
    manager.recorder.wait()
    # flatpak cannot handle concurrent transactions on one installation.
    assert manager.max_concurrent == 1


def test_the_queue_keeps_request_order(manager):
    manager.recorder.expected = 3
    for ref in ("org.a.A", "org.b.B", "org.c.C"):
        manager.install(ref)
    manager.recorder.wait()
    assert manager.started == ["org.a.A", "org.b.B", "org.c.C"]


def test_waiting_jobs_report_their_position(manager, monkeypatch):
    # Hold the first job open so the second is provably still waiting when
    # it is enqueued; otherwise this races the 50ms of fake work.
    gate = threading.Event()

    def blocking(ref):
        manager.started.append(ref)
        gate.wait(5)

    monkeypatch.setattr(manager, "_do_install", blocking)
    manager.recorder.expected = 2
    manager.install("org.a.A")
    for _ in range(200):
        if manager.started:
            break
        time.sleep(0.01)
    manager.install("org.b.B")
    gate.set()
    manager.recorder.wait()

    queued = [p for p in manager.recorder.progress if "Queued" in p[2]]
    assert queued and queued[0][0] == "org.b.B"
    assert "1 ahead" in queued[0][2]


def test_unapproved_apps_are_refused_before_queueing(manager):
    with pytest.raises(AppError, match="not on the approved app list"):
        manager.install("com.spotify.Client")
    assert manager.pending == set()


def test_the_same_app_cannot_be_queued_twice(manager):
    manager.recorder.expected = 1
    manager.install("org.a.A")
    with pytest.raises(AppError, match="already in progress"):
        manager.install("org.a.A")
    manager.recorder.wait()


def test_a_failing_job_reports_and_frees_the_queue(manager, monkeypatch):
    def explode(ref):
        raise RuntimeError("flatpak said no")

    monkeypatch.setattr(manager, "_do_install", explode)
    manager.recorder.expected = 1
    manager.install("org.a.A")
    manager.recorder.wait()
    ref, ok, error = manager.recorder.finished[0]
    assert ref == "org.a.A" and not ok and "flatpak said no" in error
    # The failure must not wedge the queue.
    assert manager.pending == set()
    manager.recorder.expected = 2
    manager.install("org.b.B")
    manager.recorder.wait()


def test_removals_are_queued_alongside_installs(manager):
    manager.recorder.expected = 2
    manager.install("org.a.A")
    manager.remove("org.b.B")
    manager.recorder.wait()
    assert manager.max_concurrent == 1
    assert sorted(manager.recorder.refs_finished) == ["org.a.A", "org.b.B"]


def test_removal_is_not_limited_to_approved_apps(manager):
    # An app can be unapproved after it was installed; the admin must still
    # be able to remove it.
    manager.recorder.expected = 1
    manager.remove("com.spotify.Client")
    manager.recorder.wait()
    assert manager.recorder.finished[0][1] is True


def test_an_update_is_queued_like_an_install_and_reported_on_the_same_signals():
    from kosherd import apps

    seen = []
    done = []
    manager = apps.AppManager(lambda *a: seen.append(a), lambda *a: done.append(a))
    manager._do_update = lambda ref: seen.append((ref, 50, "Updating…"))  # no flatpak here
    manager.update("org.example.App")
    manager._worker.join(timeout=5)
    assert ("org.example.App", 50, "Updating…") in seen
    assert done == [("org.example.App", True, "")]
