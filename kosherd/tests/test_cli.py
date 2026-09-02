

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
    assert "redirect to :8080" in out, "the interception rule is missing"
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

def test_setup_questions_go_to_the_terminal(tmp_path, monkeypatch):
    """The setup session pipes stdout through tee into the journal.

    A prompt has no newline, so it sat in journald's stream buffer and the
    person saw a banner and then silence — the boot test typed a password
    into nothing. getpass never had the problem because it opens /dev/tty,
    which is why "Password:" appeared in the old console log while
    "Username:" did not. The questions now do what getpass does.
    """
    import io

    from kosherd.cli import _Console

    tty = io.StringIO()
    tty.readline = lambda: "avraham\n"

    console = _Console.__new__(_Console)
    console.tty = tty
    answer = console.ask("Username: ")
    assert answer == "avraham"
    assert tty.getvalue() == "Username: "  # written to the tty, unbuffered


def test_a_password_is_read_without_echo_from_the_same_terminal(monkeypatch):
    # getpass reads /dev/tty and, with no controlling terminal, falls back
    # to stdin with a warning and full echo — a password typed in the clear
    # on the family setup console. ask_secret uses the terminal we have.
    from kosherd.cli import _Console

    class FakeTTY:
        def __init__(self):
            self.written = []

        def write(self, text):
            self.written.append(text)

        def flush(self):
            pass

        def readline(self):
            return "hunter2\n"

        def fileno(self):
            return -1

    console = _Console.__new__(_Console)
    console.tty = FakeTTY()
    monkeypatch.setattr("termios.tcgetattr", lambda fd: [0, 0, 0, 0xFFFF])
    flips = []
    monkeypatch.setattr("termios.tcsetattr",
                        lambda fd, when, attrs: flips.append(attrs[3]))
    secret = console.ask_secret("Password: ")
    assert secret == "hunter2"
    # Echo was cleared for the read and restored afterwards.
    assert flips and flips[0] == 0xFFFF & ~__import__("termios").ECHO
    assert flips[-1] == 0xFFFF


def test_setup_still_works_with_no_terminal_at_all(monkeypatch):
    # A scripted run in a pipeline has no /dev/tty; falling back to stdin
    # keeps it drivable.
    from kosherd.cli import _Console

    console = _Console.__new__(_Console)
    console.tty = None
    monkeypatch.setattr("builtins.input", lambda prompt: "scripted")
    assert console.ask("Username: ") == "scripted"


def test_what_was_asked_and_answered_reaches_the_log(capsys):
    from kosherd.cli import _Console

    import io

    tty = io.StringIO()
    tty.readline = lambda: "avraham\n"
    console = _Console.__new__(_Console)
    console.tty = tty
    console.ask("Username: ")
    console.say("Created avraham (uid 1000).")
    out = capsys.readouterr().out
    # The record shows the conversation; a machine whose setup failed has
    # no account to log in with, so the log is the only witness.
    assert "Username: avraham" in out
    assert "Created avraham" in out
