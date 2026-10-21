"""One change, one password (#181).

polkit keeps an answered prompt per action, so a command that read a
setting and then changed one asked an administrator's password twice on
any account that is not an administrator's. kosherctl now opens the
session first, the way the admin app does, and kosherd skips polkit for
that uid while the session lasts.
"""

from __future__ import annotations

import pytest

pytest.importorskip("gi")

from kosherd import cli  # noqa: E402
from kosherd.client import DaemonClient  # noqa: E402


class FakeConnection:
    def __init__(self):
        self.methods: list[str] = []

    def call_sync(self, _bus, _path, _iface, method, params, *_rest):
        self.methods.append(method)
        return None


def _client(unlock_first):
    client = DaemonClient.__new__(DaemonClient)
    client._conn = FakeConnection()
    client._unlock_first = unlock_first
    return client


def test_a_read_then_a_change_opens_the_session_once_first():
    client = _client(True)
    client._call("Guardian", "IsEnabled")
    client._call("Profiles", "SetFilterMode", "(iss)", 1001, "filtered", "")
    client._call("Profiles", "GetPolicy")
    assert client._conn.methods == ["Unlock", "IsEnabled", "SetFilterMode", "GetPolicy"]


def test_free_calls_do_not_open_it():
    client = _client(True)
    client._call("Profiles", "GetMySettings")
    client._call("Apps", "ListStoreApps")
    assert client._conn.methods == ["GetMySettings", "ListStoreApps"]


def test_an_app_that_unlocks_for_itself_is_left_alone():
    client = _client(False)
    client._call("Profiles", "GetPolicy")
    assert client._conn.methods == ["GetPolicy"]


def test_kosherctl_uses_one_client_that_opens_the_session(monkeypatch):
    made = []

    class Recording:
        def __init__(self, *, unlock_first=False):
            made.append(unlock_first)

    monkeypatch.setattr("kosherd.client.DaemonClient", Recording)
    monkeypatch.setattr(cli, "_shared_client", None)
    monkeypatch.setattr(cli.os, "geteuid", lambda: 1001)
    assert cli._client() is cli._client()
    assert made == [True]


def test_root_needs_no_session(monkeypatch):
    made = []

    class Recording:
        def __init__(self, *, unlock_first=False):
            made.append(unlock_first)

    monkeypatch.setattr("kosherd.client.DaemonClient", Recording)
    monkeypatch.setattr(cli, "_shared_client", None)
    monkeypatch.setattr(cli.os, "geteuid", lambda: 0)
    cli._client()
    assert made == [False]
