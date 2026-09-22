"""The daemon keeps after the store's app list.

The laptop at 0.6.4 showed an administrator the fifty approved apps and
nothing else: the index had been deployed under the old allow-list filter
(flatpak cuts denied apps out of the appstream it deploys), and the
daemon would not have asked for a fresh copy for a day. Separately, the
first fetch at boot usually runs before the network is up, and one failed
attempt left the store empty for six hours.
"""

from __future__ import annotations

import json

from kosherd import apps, daemon as daemon_mod
from kosherd.daemon import Daemon
from kosherd.policy import Policy


class _GLib:
    """Records what the daemon schedules instead of running a main loop."""

    def __init__(self):
        self.idle: list = []
        self.timeouts: list[tuple[int, object]] = []

    def idle_add(self, fn, *args):
        self.idle.append(fn)
        return 1

    def timeout_add_seconds(self, seconds, fn, *args):
        self.timeouts.append((seconds, fn))
        return 1


def _daemon(monkeypatch, warm_result):
    glib = _GLib()
    monkeypatch.setattr(daemon_mod, "GLib", type("G", (), {
        "idle_add": staticmethod(glib.idle_add),
        "timeout_add_seconds": staticmethod(glib.timeout_add_seconds),
        "Variant": daemon_mod.GLib.Variant,
    }))
    monkeypatch.setattr(apps, "warm_index", lambda: warm_result)
    d = Daemon.__new__(Daemon)
    d.policy = Policy()
    d.connection = None
    return d, glib


def _run(d):
    started = d._warm_app_index()
    d._index_thread.join(5)
    return started


def test_a_failed_refresh_is_tried_again_soon_not_in_six_hours(monkeypatch):
    d, glib = _daemon(monkeypatch, (0, False))
    assert _run(d)
    for fn in glib.idle:
        fn()
    assert [s for s, _fn in glib.timeouts] == [daemon_mod.APP_INDEX_RETRY_SECONDS]
    assert daemon_mod.APP_INDEX_RETRY_SECONDS < daemon_mod.APP_INDEX_REFRESH_SECONDS


def test_a_stale_copy_is_used_and_still_refreshed_again_soon(monkeypatch):
    # Offline at boot with yesterday's index: the fifty apps are shown,
    # and the daemon keeps trying for the rest.
    d, glib = _daemon(monkeypatch, (50, False))
    applied = []
    monkeypatch.setattr(d, "_apply_mct", lambda: applied.append(1))
    _run(d)
    for fn in glib.idle:
        fn()
    assert applied, "what is known is enforced now"
    assert [s for s, _fn in glib.timeouts] == [daemon_mod.APP_INDEX_RETRY_SECONDS]


def test_a_fresh_index_schedules_no_retry(monkeypatch):
    d, glib = _daemon(monkeypatch, (3000, True))
    monkeypatch.setattr(d, "_apply_mct", lambda: None)
    _run(d)
    for fn in glib.idle:
        fn()
    assert glib.timeouts == []


def test_only_one_retry_is_pending_at_a_time(monkeypatch):
    d, glib = _daemon(monkeypatch, (0, False))
    d._retry_app_index_soon()
    d._retry_app_index_soon()
    assert len(glib.timeouts) == 1
    # When it fires, another failure may schedule the next one.
    glib.timeouts[0][1]()
    d._index_thread.join(5)
    for fn in glib.idle:
        fn()
    assert len(glib.timeouts) == 2


def test_opening_the_store_with_no_index_goes_to_look_now(monkeypatch):
    d, _glib = _daemon(monkeypatch, (0, False))
    monkeypatch.setattr(apps, "store_apps", lambda account: {
        "access": "store", "ready": False, "apps": []})
    warmed = []
    monkeypatch.setattr(d, "_warm_app_index", lambda: warmed.append(1) or True)
    reply = json.loads(d.impl_ListStoreApps(_uid=1000).unpack()[0])
    assert reply["ready"] is False
    assert warmed == [1]
    monkeypatch.setattr(apps, "store_apps", lambda account: {
        "access": "store", "ready": True, "apps": []})
    d.impl_ListStoreApps(_uid=1000)
    assert warmed == [1], "a store that is ready asks for nothing"
