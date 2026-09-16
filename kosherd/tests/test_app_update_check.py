"""Checking for app updates: ask the remote first, then say what is new.

`available_updates` reads what flatpak already knows, and nothing on a
KosherOS machine fetches that by itself — so the store's Updates shelf
could sit empty forever and the feature looked missing.
"""

from __future__ import annotations

import json

from kosherd import access, apps
from kosherd.daemon import Daemon


class _Installation:
    def __init__(self, fail: str = ""):
        self.calls: list[str] = []
        self.fail = fail

    def update_remote_sync(self, remote, cancellable):
        self.calls.append(f"update_remote_sync:{remote}")
        if self.fail == "update_remote_sync":
            raise RuntimeError("network is down")
        return True

    def drop_caches(self, cancellable):
        self.calls.append("drop_caches")
        if self.fail == "drop_caches":
            raise RuntimeError("no")
        return True


def _fake_flatpak(monkeypatch, installation):
    fake = type("F", (), {"Installation": type("I", (), {
        "new_system": staticmethod(lambda _c: installation)})})
    monkeypatch.setattr(apps, "Flatpak", fake)


def test_the_check_refreshes_the_remote_before_it_looks(monkeypatch):
    installation = _Installation()
    _fake_flatpak(monkeypatch, installation)
    monkeypatch.setattr(apps, "available_updates", lambda: [{"ref": "org.x.App"}])
    assert apps.refresh_updates() == [{"ref": "org.x.App"}]
    assert installation.calls == ["update_remote_sync:flathub", "drop_caches"]


def test_a_refresh_that_fails_still_answers_with_what_is_known(monkeypatch):
    installation = _Installation(fail="update_remote_sync")
    _fake_flatpak(monkeypatch, installation)
    monkeypatch.setattr(apps, "available_updates", lambda: [])
    assert apps.refresh_updates() == []
    assert "drop_caches" in installation.calls, "one failure does not stop the rest"


def test_without_flatpak_the_answer_is_nothing_not_a_crash(monkeypatch):
    monkeypatch.setattr(apps, "Flatpak", None)
    assert apps.refresh_updates() == []


def test_the_daemon_offers_the_check_to_anyone_who_may_use_the_store():
    assert access.ACTIONS["CheckAppUpdates"] == access.ACTION_USE_STORE


def test_the_daemon_answers_with_json_and_never_raises(monkeypatch):
    daemon = Daemon.__new__(Daemon)
    monkeypatch.setattr(apps, "refresh_updates", lambda: [{"ref": "org.x.App", "name": "App"}])
    assert json.loads(daemon.impl_CheckAppUpdates().unpack()[0]) == [
        {"ref": "org.x.App", "name": "App"}]

    def boom():
        raise RuntimeError("flatpak is unwell")

    monkeypatch.setattr(apps, "refresh_updates", boom)
    assert json.loads(daemon.impl_CheckAppUpdates().unpack()[0]) == []
