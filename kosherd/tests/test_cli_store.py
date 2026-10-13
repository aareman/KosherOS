"""kosherctl store: what is installed from the Store, and updating it.

Over ssh, in a VM suite or on a support call, "what is installed and is
it current" needs an answer from a shell; the Store window had it alone.
"""

import pytest

from kosherd import cli


def _args(action, apps=(), check=False, all=False):
    return type("A", (), {"action": action, "apps": list(apps), "check": check, "all": all})()


class _Client:
    def __init__(self):
        self.calls = []
        self.details = [
            {"ref": "org.gnome.Chess", "name": "Chess", "size": 41 * 1024 ** 2, "installed_by": "avi"},
            {"ref": "nixpkgs#gh", "name": "GitHub CLI", "size": 0, "installed_by": "wlkid"},
            {"ref": "org.mozilla.firefox", "name": "Firefox", "size": 1.3 * 1024 ** 3, "installed_by": ""},
        ]
        self.updates = [{"ref": "org.gnome.Chess", "name": "Chess", "version": "43.1",
                         "commit": "abc", "latest": "def"}]

    def list_installed_details(self):
        self.calls.append("details")
        return self.details

    def list_installed(self):
        self.calls.append("installed")
        return [a["ref"] for a in self.details]

    def list_app_updates(self):
        self.calls.append("list_updates")
        return self.updates

    def check_app_updates(self):
        self.calls.append("check_updates")
        return self.updates

    def update_app(self, ref):
        self.calls.append(("update", ref))

    def update_all_apps(self):
        self.calls.append("update_all")
        return len(self.updates)


@pytest.fixture
def client(monkeypatch):
    c = _Client()
    monkeypatch.setattr(cli, "_client", lambda: c)
    return c


def test_installed_lists_every_app_with_size_and_who_asked(client, capsys):
    assert cli.cmd_store(_args("installed")) == 0
    out = capsys.readouterr().out
    lines = [l for l in out.splitlines() if l.startswith("  ")]
    # By name: Chess, Firefox, GitHub CLI.
    assert [l.split()[0] for l in lines] == ["org.gnome.Chess", "org.mozilla.firefox", "nixpkgs#gh"]
    assert "41 MB" in lines[0] and "avi" in lines[0]
    assert "1.3 GB" in lines[1]
    assert lines[2].rstrip().endswith("-  wlkid")   # a nixpkgs app has no size the daemon knows
    assert "3 installed" in out


def test_installed_falls_back_to_refs_on_an_older_daemon(client, capsys):
    def boom():
        raise RuntimeError("no such method")
    client.list_installed_details = boom
    assert cli.cmd_store(_args("installed")) == 0
    out = capsys.readouterr().out
    assert "org.gnome.Chess" in out and "3 installed" in out


def test_installed_says_so_when_there_is_nothing(client, capsys):
    client.details = []
    assert cli.cmd_store(_args("installed")) == 0
    assert "nothing is installed" in capsys.readouterr().out


def test_updates_reads_what_the_computer_knows_and_check_asks_the_store(client, capsys):
    assert cli.cmd_store(_args("updates")) == 0
    out = capsys.readouterr().out
    assert client.calls == ["list_updates"]
    assert "org.gnome.Chess" in out and "43.1 -> def" in out and "1 with an update" in out
    assert cli.cmd_store(_args("updates", check=True)) == 0
    assert client.calls[-1] == "check_updates"


def test_updates_says_up_to_date_and_how_to_be_sure(client, capsys):
    client.updates = []
    assert cli.cmd_store(_args("updates")) == 0
    out = capsys.readouterr().out
    assert "up to date" in out and "--check" in out
    assert cli.cmd_store(_args("updates", check=True)) == 0
    assert "--check" not in capsys.readouterr().out


def test_update_queues_named_apps_or_everything(client, capsys):
    assert cli.cmd_store(_args("update", apps=["org.gnome.Chess", "org.mozilla.firefox"])) == 0
    assert client.calls == [("update", "org.gnome.Chess"), ("update", "org.mozilla.firefox")]
    out = capsys.readouterr().out
    assert "queued org.gnome.Chess" in out and "queued org.mozilla.firefox" in out
    assert cli.cmd_store(_args("update", all=True)) == 0
    assert client.calls[-1] == "update_all"
    assert "queued 1 update" in capsys.readouterr().out


def test_update_with_nothing_named_is_a_usage_error(client, capsys):
    assert cli.cmd_store(_args("update")) == 2
    assert "usage" in capsys.readouterr().err
    assert client.calls == []


def test_the_command_line_reaches_the_store_command(client, capsys):
    assert cli.main(["store", "update", "--all"]) == 0
    assert client.calls == ["update_all"]
    assert cli.main(["store", "updates", "--check"]) == 0
    assert client.calls[-1] == "check_updates"
    assert cli.main(["store", "installed"]) == 0
    assert "3 installed" in capsys.readouterr().out
