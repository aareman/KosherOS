

def test_rendering_offline_produces_the_ruleset_that_is_applied(tmp_path,
                                                                monkeypatch,
                                                                capsys):
    """The offline renderer used to leave out the redirect into the proxy.

    It passed only dns_uid, so `kosherctl render-nft` printed a ruleset
    without the interception rule, the search back end guard or the plain
    resolver — while being the command documented as the fast loop for
    enforcement changes. A missing line is the hardest kind of difference
    to notice.
    """
    import json
    import pwd

    from kosherd import cli

    policy = tmp_path / "policy.json"
    policy.write_text(json.dumps({
        "schema_version": 1, "revision": 1, "source": "local",
        "guardian": {"enabled": False},
        "users": [{"uid": 1001, "username": "a", "mode": "filtered"}]}))

    fake = {"kosher-mitm": 988, "kosher-search": 987, "dnsmasq": 989}

    def getpwnam(name):
        if name not in fake:
            raise KeyError(name)
        return type("P", (), {"pw_uid": fake[name]})()

    monkeypatch.setattr(pwd, "getpwnam", getpwnam)
    cli.main(["render-nft", str(policy)])
    out = capsys.readouterr().out
    assert "redirect to :30000" in out, "the interception rule is missing"
    assert "8889" in out, "the search back end guard is missing"


def test_a_missing_service_user_is_stated_not_silently_dropped(tmp_path,
                                                               monkeypatch,
                                                               capsys):
    import json
    import pwd

    from kosherd import cli

    policy = tmp_path / "policy.json"
    policy.write_text(json.dumps({
        "schema_version": 1, "revision": 1, "source": "local",
        "guardian": {"enabled": False},
        "users": [{"uid": 1001, "username": "a", "mode": "filtered"}]}))

    def getpwnam(name):
        if name == "dnsmasq":
            return type("P", (), {"pw_uid": 989})()
        raise KeyError(name)

    monkeypatch.setattr(pwd, "getpwnam", getpwnam)
    cli.main(["render-nft", str(policy)])
    out = capsys.readouterr().out
    assert "no kosher-mitm user" in out
    assert "no kosher-search user" in out


# -- talking to the person, not to the log ------------------------------------

import io  # noqa: E402
import sys  # noqa: E402

import pytest  # noqa: E402

from kosherd import cli  # noqa: E402


def test_the_prompt_appears_before_the_answer_is_read(monkeypatch, capsys):
    """The bug that took three boots: input()'s prompt has no newline, and
    stdout is a pipe to tee, so it sat unflushed while readline blocked and
    the person saw the banner then silence. ask() writes and flushes.
    """
    monkeypatch.setattr(sys, "stdin", io.StringIO("avraham\n"))
    answer = cli._Console().ask("Username: ")
    assert answer == "avraham"
    assert "Username: " in capsys.readouterr().out


def test_the_answer_is_read_from_stdin(monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO("shloimy\n"))
    assert cli._Console().ask("Username: ") == "shloimy"


def test_a_closed_console_is_an_error_not_a_hang(monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))  # EOF immediately
    with pytest.raises(EOFError):
        cli._Console().ask("Username: ")


def test_console_lines_survive_crlf_endings(monkeypatch):
    # The boot mismatch: virtio-serial delivered a trailing carriage
    # return that rstrip("\n") left in place, so "pw\r" != "pw" and the
    # two password entries never matched. A real console's line endings
    # vary (\n, \r\n, \r); strip them all.
    for raw, want in [("boottest\n", "boottest"),
                      ("boottest\r\n", "boottest"),
                      ("boottest\r", "boottest"),
                      ("boot-test-pw-1\r\n", "boot-test-pw-1")]:
        monkeypatch.setattr(sys, "stdin", io.StringIO(raw))
        assert cli._Console().ask("x: ") == want, raw


def test_two_entries_with_different_endings_still_match(monkeypatch):
    # Exactly the boot failure: same password, one read with a CR and one
    # without, must compare equal.
    monkeypatch.setattr(sys, "stdin", io.StringIO("secret\r\n"))
    first = cli._Console().ask("Password: ")
    monkeypatch.setattr(sys, "stdin", io.StringIO("secret\n"))
    second = cli._Console().ask("Confirm: ")
    assert first == second == "secret"


def test_a_password_falls_back_to_getpass_without_a_tty(monkeypatch):
    # A pipe or a test stdin has no fileno; that is not a tty, and the
    # secret path must fall back rather than raise. The echo-off branch is
    # only reachable on a real console and is covered by the boot test.
    monkeypatch.setattr(sys, "stdin", io.StringIO("typed\n"))
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt: "secret")
    assert cli._Console().ask_secret("Password: ") == "secret"


def test_the_diagnostic_records_whether_stdin_is_a_tty(monkeypatch, caplog):
    # A boot that still cannot prompt should say why in the log rather than
    # hang in silence — this is the line that would tell us.
    import logging

    # A StringIO has no fileno; _isatty must treat that as "not a tty"
    # rather than raise, and the diagnostic must still be logged.
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    with caplog.at_level(logging.INFO, logger="kosherd.cli"):
        cli._Console()
    assert any("isatty" in r.message for r in caplog.records)


def test_the_activity_command_prints_the_filters_diary(monkeypatch, capsys):
    import time

    from kosherd import cli

    now = int(time.time())

    class Client:
        def get_policy(self):
            return {"users": [{"uid": 1001, "username": "yosef"}]}

        def list_activity(self, since, uid):
            assert uid == 1001
            return [{"t": now - 60, "kind": "block", "uid": 1001, "username": "yosef",
                     "url": "https://roblox.com/", "why": "category:games"},
                    {"t": now - 120, "kind": "change", "uid": 1001, "username": "yosef",
                     "by": 1000, "by_username": "avi", "method": "SetFilterMode",
                     "args": [1001, "filtered"], "guardian": True}]

    monkeypatch.setattr(cli, "_client", lambda: Client())
    args = type("A", (), {"user": "yosef", "days": 1})()
    assert cli.cmd_activity(args) == 0
    out = capsys.readouterr().out
    assert "roblox.com" in out and "category:games" in out
    assert "avi changed yosef: SetFilterMode  (guardian)" in out


def test_the_activity_command_refuses_an_unknown_account(monkeypatch, capsys):
    from kosherd import cli

    class Client:
        def get_policy(self):
            return {"users": []}

    monkeypatch.setattr(cli, "_client", lambda: Client())
    args = type("A", (), {"user": "nobody", "days": 1})()
    assert cli.cmd_activity(args) == 1
    assert "nobody" in capsys.readouterr().err
