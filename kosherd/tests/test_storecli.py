"""`kosher`: the Store from a terminal, the way apt and dnf are used.

The daemon's rules are unchanged; what this adds is naming an app the
way a person does, and watching the job rather than being told it was
queued.
"""

import pytest

from kosherd import storecli

STORE = [
    {"ref": "org.gnome.Chess", "name": "Chess", "summary": "Play chess", "kind": "games"},
    {"ref": "org.gnome.Calculator", "name": "Calculator", "summary": "Everyday calculations", "kind": "utilities"},
    {"ref": "nixpkgs#claude-code", "name": "Claude Code", "summary": "Anthropic's coding agent, in the terminal", "kind": "develop"},
    {"ref": "nixpkgs#gh", "name": "GitHub CLI", "summary": "GitHub from the terminal", "kind": "develop"},
    {"ref": "com.visualstudio.code", "name": "Visual Studio Code", "summary": "A code editor", "kind": "develop"},
    {"ref": "nixpkgs#vscode", "name": "Visual Studio Code (for Dev Containers)", "summary": "Outside the sandbox", "kind": "develop"},
]


class _Client:
    def __init__(self, installed=("org.gnome.Calculator",), updates=()):
        self.calls = []
        self.installed = list(installed)
        self.updates = list(updates)

    def list_store_apps(self):
        return {"apps": STORE, "access": "approved", "ready": True}

    def list_installed(self):
        return list(self.installed)

    def list_installed_details(self):
        return [{"ref": r, "name": next((a["name"] for a in STORE if a["ref"] == r), r),
                 "installed_by": "avi"} for r in self.installed]

    def install_app(self, ref):
        self.calls.append(("install", ref))

    def remove_app(self, ref):
        self.calls.append(("remove", ref))

    def check_app_updates(self):
        self.calls.append("check")
        return self.updates

    def list_app_updates(self):
        return self.updates

    def update_app(self, ref):
        self.calls.append(("update", ref))


def _wait_ok(client, refs, out=None):
    client.calls.append(("wait", tuple(refs)))
    return True


def _args(**kw):
    return type("A", (), kw)()


# -- naming -------------------------------------------------------------------

@pytest.mark.parametrize("word,ref", [
    ("org.gnome.Chess", "org.gnome.Chess"),         # the id
    ("chess", "org.gnome.Chess"),                   # the id's last part
    ("Claude Code", "nixpkgs#claude-code"),         # the name
    ("claude", "nixpkgs#claude-code"),              # enough of the name
    ("gh", "nixpkgs#gh"),                           # a nixpkgs id's last part
    ("CALCULATOR", "org.gnome.Calculator"),         # case does not matter
])
def test_an_app_is_named_the_way_a_person_names_it(word, ref):
    assert storecli.resolve(word, STORE)["ref"] == ref


def test_an_ambiguous_word_lists_the_candidates():
    found = storecli.resolve("visual studio", STORE)
    assert isinstance(found, list) and {a["ref"] for a in found} == {"com.visualstudio.code", "nixpkgs#vscode"}
    # ...but the exact name still wins.
    assert storecli.resolve("Visual Studio Code", STORE)["ref"] == "com.visualstudio.code"


def test_an_unknown_word_is_nothing():
    assert storecli.resolve("minecraft", STORE) == []
    assert storecli.resolve("", STORE) == []


# -- install, remove, upgrade watch the job --------------------------------------

def test_install_resolves_names_and_watches(capsys):
    c = _Client()
    assert storecli.cmd_install(_args(apps=["claude", "chess"]), c, wait=_wait_ok) == 0
    assert c.calls == [("install", "nixpkgs#claude-code"), ("install", "org.gnome.Chess"),
                       ("wait", ("nixpkgs#claude-code", "org.gnome.Chess"))]
    assert "installing nixpkgs#claude-code" in capsys.readouterr().out


def test_install_skips_what_is_already_there(capsys):
    c = _Client(installed=["org.gnome.Chess"])
    assert storecli.cmd_install(_args(apps=["chess"]), c, wait=_wait_ok) == 0
    assert c.calls == []
    assert "already installed" in capsys.readouterr().out


def test_install_says_what_the_daemon_said(capsys):
    class Refusing(_Client):
        def install_app(self, ref):
            raise RuntimeError("GDBus.Error:org.kosherlinux.Daemon1.Error: org.gnome.Chess is not on the approved list")
    assert storecli.cmd_install(_args(apps=["chess"]), Refusing(), wait=_wait_ok) == 1
    assert "not on the approved list" in capsys.readouterr().err


def test_install_of_an_unknown_or_ambiguous_app_says_so(capsys):
    c = _Client()
    assert storecli.cmd_install(_args(apps=["minecraft"]), c, wait=_wait_ok) == 1
    assert "kosher search minecraft" in capsys.readouterr().err
    assert storecli.cmd_install(_args(apps=["visual studio"]), c, wait=_wait_ok) == 1
    err = capsys.readouterr().err
    assert "could be any of these" in err and "nixpkgs#vscode" in err
    assert c.calls == []


def test_remove_names_what_is_installed(capsys):
    c = _Client(installed=["org.gnome.Calculator", "nixpkgs#gh"])
    assert storecli.cmd_remove(_args(apps=["gh"]), c, wait=_wait_ok) == 0
    assert c.calls == [("remove", "nixpkgs#gh"), ("wait", ("nixpkgs#gh",))]
    # Not something that is not installed.
    assert storecli.cmd_remove(_args(apps=["chess"]), c, wait=_wait_ok) == 1


def test_update_asks_the_store_and_upgrade_takes_what_it_found(capsys):
    c = _Client(installed=["org.gnome.Calculator"],
                updates=[{"ref": "org.gnome.Calculator", "name": "Calculator", "version": "46", "latest": "47"}])
    assert storecli.cmd_update(_args(), c) == 0
    out = capsys.readouterr().out
    assert c.calls == ["check"] and "46 -> 47" in out and "kosher upgrade" in out
    assert storecli.cmd_upgrade(_args(apps=[]), c, wait=_wait_ok) == 0
    assert c.calls[-2:] == [("update", "org.gnome.Calculator"), ("wait", ("org.gnome.Calculator",))]
    assert storecli.cmd_upgrade(_args(apps=["calculator"]), c, wait=_wait_ok) == 0


def test_upgrade_with_nothing_to_do_points_at_update(capsys):
    assert storecli.cmd_upgrade(_args(apps=[]), _Client(), wait=_wait_ok) == 0
    assert "kosher update" in capsys.readouterr().out


# -- search, list, info -----------------------------------------------------------

def test_search_matches_names_ids_and_summaries(capsys):
    assert storecli.cmd_search(_args(words=["terminal"]), _Client()) == 0
    out = capsys.readouterr().out
    assert "nixpkgs#claude-code" in out and "nixpkgs#gh" in out and "Chess" not in out
    assert storecli.cmd_search(_args(words=["minecraft"]), _Client()) == 1


def test_list_shows_what_is_installed_and_for_whom(capsys):
    assert storecli.cmd_list(_args(), _Client(installed=["nixpkgs#gh", "org.gnome.Chess"])) == 0
    out = capsys.readouterr().out
    assert out.index("org.gnome.Chess") < out.index("nixpkgs#gh")   # by name: Chess, GitHub CLI
    assert "avi" in out


def test_info_says_how_an_app_installs(capsys):
    assert storecli.cmd_info(_args(app="claude"), _Client()) == 0
    out = capsys.readouterr().out
    assert "Nix, into your own profile" in out and "installed: no" in out
    assert storecli.cmd_info(_args(app="calculator"), _Client()) == 0
    out = capsys.readouterr().out
    assert "Flatpak, from Flathub" in out and "installed: yes" in out


# -- the command line ----------------------------------------------------------------

def test_the_command_line_reaches_the_commands(monkeypatch, capsys):
    c = _Client()
    monkeypatch.setattr(storecli, "_client", lambda: c)
    monkeypatch.setattr(storecli, "_wait_for", _wait_ok)
    assert storecli.main(["install", "gh"]) == 0
    assert c.calls[0] == ("install", "nixpkgs#gh")
    assert storecli.main(["list"]) == 0
    assert storecli.main(["search", "chess"]) == 0


def test_a_daemon_refusal_is_one_plain_line(monkeypatch, capsys):
    class Error(Exception):
        message = "GDBus.Error:org.kosherlinux.Daemon1.Error: not authorized for org.kosherlinux.use-store"

    class Refusing(_Client):
        def list_store_apps(self):
            raise Error()
    monkeypatch.setattr(storecli, "_client", lambda: Refusing())
    assert storecli.main(["search", "chess"]) == 1
    assert capsys.readouterr().err.strip() == "not authorized for org.kosherlinux.use-store"
