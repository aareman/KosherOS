

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
