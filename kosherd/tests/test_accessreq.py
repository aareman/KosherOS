"""Asking for a page.

Every filter is wrong sometimes; what decides whether a family keeps using
one is what happens next. These tests are mostly about the queue not being
a way in: a request carries no authority, and the ids it hands back get
turned into file paths.
"""

import json

import pytest

from kosherd import accessreq


def test_a_request_is_recorded_and_comes_back(tmp_path):
    request_id = accessreq.submit(1001, "https://example.com/needed",
                                  "for school", spool=tmp_path)
    waiting = accessreq.pending(spool=tmp_path)
    assert len(waiting) == 1
    assert waiting[0]["id"] == request_id
    assert waiting[0]["uid"] == 1001
    assert waiting[0]["url"] == "https://example.com/needed"
    assert waiting[0]["note"] == "for school"


def test_resolving_takes_it_off_the_queue(tmp_path):
    request_id = accessreq.submit(1001, "https://example.com/", spool=tmp_path)
    assert accessreq.resolve(request_id, spool=tmp_path)["id"] == request_id
    assert accessreq.pending(spool=tmp_path) == []
    assert accessreq.resolve(request_id, spool=tmp_path) is None


def test_requests_come_back_oldest_first(tmp_path):
    first = accessreq.submit(1001, "https://a.example/", spool=tmp_path)
    second = accessreq.submit(1002, "https://b.example/", spool=tmp_path)
    import os
    import time
    # Same second otherwise, and the order is what an admin works through.
    os.utime(tmp_path / f"{second}.json", None)
    doc = json.loads((tmp_path / f"{second}.json").read_text())
    doc["asked"] = doc["asked"] + 10
    (tmp_path / f"{second}.json").write_text(json.dumps(doc))
    assert [r["id"] for r in accessreq.pending(spool=tmp_path)] == [first, second]


@pytest.mark.parametrize("url", [
    "", "   ", "file:///etc/shadow", "javascript:alert(1)",
    "data:text/html,<script>", "ftp://example.com/x",
])
def test_only_web_addresses_can_be_requested(tmp_path, url):
    with pytest.raises(accessreq.RequestError):
        accessreq.submit(1001, url, spool=tmp_path)


def test_an_enormous_address_is_refused(tmp_path):
    with pytest.raises(accessreq.RequestError):
        accessreq.submit(1001, "https://example.com/" + "x" * 5000,
                         spool=tmp_path)


def test_a_long_note_is_trimmed_rather_than_refused(tmp_path):
    # Refusing would lose the request over something that does not matter.
    accessreq.submit(1001, "https://example.com/", "why " * 500, spool=tmp_path)
    assert len(accessreq.pending(spool=tmp_path)[0]["note"]) <= accessreq.MAX_NOTE


@pytest.mark.parametrize("bad_id", [
    "../../../etc/passwd", "..", "", "a" * 31, "a" * 33, "ZZZZ" * 8,
    "abc/../../x", "abcdefabcdefabcdefabcdefabcdefgg",
])
def test_a_crafted_id_cannot_reach_another_file(tmp_path, bad_id):
    # Ids come back from a UI and are turned into paths.
    (tmp_path / "secret.json").write_text("{}")
    assert accessreq.resolve(bad_id, spool=tmp_path) is None
    assert (tmp_path / "secret.json").exists()


def test_an_unreadable_request_is_discarded_not_returned(tmp_path):
    (tmp_path / ("f" * 32 + ".json")).write_text("{ not json")
    assert accessreq.pending(spool=tmp_path) == []
    assert not (tmp_path / ("f" * 32 + ".json")).exists()


def test_a_request_missing_its_fields_is_discarded(tmp_path):
    (tmp_path / ("e" * 32 + ".json")).write_text(json.dumps({"id": "e" * 32}))
    assert accessreq.pending(spool=tmp_path) == []


def test_a_queue_nobody_empties_cannot_fill_the_disk(tmp_path):
    for i in range(accessreq.MAX_PENDING + 20):
        accessreq.submit(1001, f"https://example.com/{i}", spool=tmp_path)
    assert len(accessreq.pending(spool=tmp_path)) == accessreq.MAX_PENDING


def test_a_missing_spool_is_not_a_crash():
    from pathlib import Path

    assert accessreq.pending(spool=Path("/nonexistent/requests")) == []


def test_a_half_written_request_is_never_read(tmp_path):
    # submit() writes to a dot-file and renames, so a reader mid-write
    # sees nothing rather than half a document.
    (tmp_path / ".partial.tmp").write_text('{"id": "x"')
    assert accessreq.pending(spool=tmp_path) == []


# -- what approving actually does ---------------------------------------------

def _daemon(user, tmp_path, monkeypatch):
    from kosherd import accessreq as accessreq_mod
    from kosherd.daemon import Daemon
    from kosherd.policy import Policy

    monkeypatch.setattr(accessreq_mod, "SPOOL_DIR", tmp_path)
    daemon = Daemon.__new__(Daemon)
    daemon.policy = Policy(revision=1, users=[user])
    daemon._save_and_apply = lambda: None
    return daemon


def test_approving_for_a_whitelist_account_adds_the_domain(tmp_path, monkeypatch):
    # An allow RULE would do nothing for a whitelist account: its traffic
    # never reaches the proxy. Making the admin work that out is exactly
    # the friction that gets a filter switched off.
    from kosherd.policy import UserPolicy

    user = UserPolicy(uid=1001, username="a", mode="whitelist",
                      whitelist=["chinuch.org"])
    daemon = _daemon(user, tmp_path, monkeypatch)
    request_id = accessreq.submit(1001, "https://torahanytime.com/lessons/1",
                                  spool=tmp_path)
    daemon.impl_ApproveRequest(request_id, False, "")
    assert "torahanytime.com" in user.whitelist
    assert accessreq.pending(spool=tmp_path) == []


def test_approving_for_a_filtered_account_adds_an_allow_rule(tmp_path, monkeypatch):
    from kosherd.policy import UserPolicy

    user = UserPolicy(uid=1001, username="a", mode="filtered",
                      rules=[{"action": "block", "pattern": "*"}])
    daemon = _daemon(user, tmp_path, monkeypatch)
    request_id = accessreq.submit(1001, "https://example.com/needed/page",
                                  spool=tmp_path)
    daemon.impl_ApproveRequest(request_id, False, "")
    # Ahead of what blocked it: a rule added after would never be reached.
    assert user.rules[0] == {"action": "allow",
                             "pattern": "example.com/needed/page"}
    assert user.rules[1]["action"] == "block"


def test_approving_a_whole_site_allows_the_host(tmp_path, monkeypatch):
    from kosherd.policy import UserPolicy

    user = UserPolicy(uid=1001, username="a", mode="filtered")
    daemon = _daemon(user, tmp_path, monkeypatch)
    request_id = accessreq.submit(1001, "https://example.com/needed/page",
                                  spool=tmp_path)
    daemon.impl_ApproveRequest(request_id, True, "")
    assert user.rules[0]["pattern"] == "example.com"


def test_approving_twice_does_not_pile_up_rules(tmp_path, monkeypatch):
    from kosherd.policy import UserPolicy

    user = UserPolicy(uid=1001, username="a", mode="filtered")
    daemon = _daemon(user, tmp_path, monkeypatch)
    for _ in range(2):
        request_id = accessreq.submit(1001, "https://example.com/x",
                                      spool=tmp_path)
        daemon.impl_ApproveRequest(request_id, False, "")
    assert len(user.rules) == 1


def test_approving_a_request_that_is_gone_says_so(tmp_path, monkeypatch):
    from kosherd.daemon import PolicyError
    from kosherd.policy import UserPolicy

    user = UserPolicy(uid=1001, username="a", mode="filtered")
    daemon = _daemon(user, tmp_path, monkeypatch)
    with pytest.raises(PolicyError):
        daemon.impl_ApproveRequest("a" * 32, False, "")


def test_a_request_from_an_account_that_is_gone_is_refused(tmp_path, monkeypatch):
    from kosherd.daemon import PolicyError
    from kosherd.policy import UserPolicy

    user = UserPolicy(uid=1001, username="a", mode="filtered")
    daemon = _daemon(user, tmp_path, monkeypatch)
    request_id = accessreq.submit(4242, "https://example.com/x", spool=tmp_path)
    with pytest.raises(PolicyError):
        daemon.impl_ApproveRequest(request_id, False, "")


def test_dismissing_changes_nothing_but_clears_the_queue(tmp_path, monkeypatch):
    from kosherd.policy import UserPolicy

    user = UserPolicy(uid=1001, username="a", mode="filtered")
    daemon = _daemon(user, tmp_path, monkeypatch)
    request_id = accessreq.submit(1001, "https://example.com/x", spool=tmp_path)
    daemon.impl_DismissRequest(request_id)
    assert user.rules == []
    assert accessreq.pending(spool=tmp_path) == []


def test_a_missing_spool_gives_words_rather_than_a_traceback(tmp_path):
    # The spool is created by tmpfiles.d at boot. If it is not there, the
    # person asking must see something they can act on — asking is the one
    # thing that must not feel broken.
    with pytest.raises(accessreq.RequestError) as caught:
        accessreq.submit(1001, "https://example.com/",
                         spool=tmp_path / "does-not-exist")
    assert "could not record" in str(caught.value)
