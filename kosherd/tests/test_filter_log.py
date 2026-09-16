"""An administrator can read the filter's log without root.

Asked for the journal while chasing a YouTube block that did not happen,
the user answered "i can't run sudo" — and on KosherOS nobody can. kosherd
reads the filter's own units for them.
"""

from kosherd import access
from kosherd.daemon import Daemon


def test_the_log_is_a_read_for_administrators():
    assert access.ACTIONS["FilterLog"] == access.ACTION_READ_CONFIG


def test_only_the_filters_units_and_a_bounded_number_of_lines(monkeypatch):
    seen = {}

    def run(argv, **kw):
        seen["argv"] = argv
        return type("R", (), {"returncode": 0, "stdout": "line\n", "stderr": ""})()

    monkeypatch.setattr("kosherd.daemon.subprocess.run", run)
    daemon = Daemon.__new__(Daemon)
    assert daemon.impl_FilterLog(50).unpack()[0] == "line\n"
    argv = seen["argv"]
    assert argv[0] == "journalctl" and "--no-pager" in argv
    assert argv[argv.index("-n") + 1] == "50"
    units = [argv[i + 1] for i, a in enumerate(argv) if a == "-u"]
    assert units == list(Daemon.LOGGED_UNITS) and "kosher-mitm" in units
    daemon.impl_FilterLog(999999)
    assert seen["argv"][seen["argv"].index("-n") + 1] == "2000", "bounded"
    daemon.impl_FilterLog(0)
    assert seen["argv"][seen["argv"].index("-n") + 1] == "200", "a sensible default"


def test_the_cli_filters_by_pattern(monkeypatch, capsys):
    from kosherd import cli

    class C:
        def filter_log(self, lines):
            return "a: blocked a YouTube video (category)\nb: something else\n"

    monkeypatch.setattr(cli, "_client", lambda: C())
    assert cli.main(["log", "-g", "youtube"]) == 0
    out = capsys.readouterr().out
    assert "blocked a YouTube video" in out and "something else" not in out
    assert cli.main(["log", "--grep", "nomatch"]) == 0
    assert "nothing matching" in capsys.readouterr().out
