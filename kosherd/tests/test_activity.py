"""The activity log: what the filter did, written down for the parent."""

import json
import time

import pytest

from kosherd import activity


def test_a_block_is_recorded_and_comes_back(tmp_path):
    assert activity.record("proxy", activity.BLOCK, 1001,
                           url="https://roblox.com/", why="category:games",
                           spool=tmp_path)
    found = activity.events(spool=tmp_path)
    assert len(found) == 1
    assert found[0]["uid"] == 1001
    assert found[0]["url"] == "https://roblox.com/"
    assert found[0]["why"] == "category:games"
    assert found[0]["kind"] == "block"


def test_events_come_back_newest_first_across_writers(tmp_path):
    activity.record("proxy", activity.BLOCK, 1, url="https://a/", spool=tmp_path)
    activity.record("search", activity.SEARCH, 1, text="x", spool=tmp_path)
    # Force distinct timestamps without sleeping a second.
    lines = (tmp_path / "proxy.jsonl").read_text().splitlines()
    doc = json.loads(lines[0])
    doc["t"] -= 60
    (tmp_path / "proxy.jsonl").write_text(json.dumps(doc) + "\n")

    found = activity.events(spool=tmp_path)
    assert [d["kind"] for d in found] == ["search", "block"]


def test_each_writer_keeps_its_own_file(tmp_path):
    activity.record("proxy", activity.BLOCK, 1, url="https://a/", spool=tmp_path)
    activity.record("search", activity.SEARCH, 1, text="x", spool=tmp_path)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["proxy.jsonl",
                                                          "search.jsonl"]


def test_narrowing_to_one_account(tmp_path):
    activity.record("proxy", activity.BLOCK, 1, url="https://a/", spool=tmp_path)
    activity.record("proxy", activity.BLOCK, 2, url="https://b/", spool=tmp_path)
    assert [d["uid"] for d in activity.events(uid=2, spool=tmp_path)] == [2]


def test_only_events_since_a_moment_are_returned(tmp_path):
    activity.record("proxy", activity.BLOCK, 1, url="https://a/", spool=tmp_path)
    assert activity.events(since=int(time.time()) + 10, spool=tmp_path) == []


def test_an_unknown_kind_is_a_programming_error(tmp_path):
    with pytest.raises(ValueError):
        activity.record("proxy", "browsed", 1, url="https://a/", spool=tmp_path)


def test_a_torn_line_is_skipped_not_fatal(tmp_path):
    activity.record("proxy", activity.BLOCK, 1, url="https://a/", spool=tmp_path)
    with open(tmp_path / "proxy.jsonl", "a") as handle:
        handle.write('{"t": 1, "kind": "blo')
    assert len(activity.events(spool=tmp_path)) == 1


def test_a_missing_spool_is_not_a_crash(tmp_path):
    assert activity.events(spool=tmp_path / "nowhere") == []
    assert not activity.record("proxy", activity.BLOCK, 1, url="https://a/",
                               spool=tmp_path / "nowhere")


def test_the_summary_counts_what_the_cards_show(tmp_path):
    activity.record("proxy", activity.BLOCK, 1, url="https://a/", spool=tmp_path)
    activity.record("proxy", activity.BLOCK, 1, url="https://b/", spool=tmp_path)
    activity.record("proxy", activity.PICTURES, 1, url="https://c/", spool=tmp_path)
    activity.record("proxy", activity.VIDEO, 1, url="https://d/", spool=tmp_path)
    activity.record("search", activity.SEARCH, 2, text="q", spool=tmp_path)
    activity.record("kosherd", activity.CHANGE, 1, by=1000, method="SetFilterMode",
                    spool=tmp_path)
    counts = activity.summary(activity.events(spool=tmp_path))
    assert counts[1]["blocked"] == 3        # pages and the refused video
    assert counts[1]["pictures"] == 1
    assert counts[2]["searches"] == 1
    # A settings change is not something the filter did to the account.
    assert counts[1]["last"] > 0


def test_trimming_drops_a_week_old_line_and_keeps_the_rest(tmp_path):
    activity.record("proxy", activity.BLOCK, 1, url="https://new/", spool=tmp_path)
    old = {"t": int(time.time()) - activity.KEEP_SECONDS - 5, "kind": "block",
           "uid": 1, "url": "https://old/"}
    with open(tmp_path / "proxy.jsonl", "a") as handle:
        handle.write(json.dumps(old) + "\n" + "garbage\n")
    activity.trim(spool=tmp_path)
    found = activity.events(spool=tmp_path)
    assert [d["url"] for d in found] == ["https://new/"]
    assert "garbage" not in (tmp_path / "proxy.jsonl").read_text()


def test_trimming_leaves_a_fresh_file_alone(tmp_path):
    activity.record("proxy", activity.BLOCK, 1, url="https://a/", spool=tmp_path)
    before = (tmp_path / "proxy.jsonl").stat().st_mtime_ns
    activity.trim(spool=tmp_path)
    assert (tmp_path / "proxy.jsonl").stat().st_mtime_ns == before


def test_an_enormous_change_record_still_fits_on_a_line(tmp_path):
    activity.record("kosherd", activity.CHANGE, 1, by=1000, method="SetWhitelist",
                    args=[f"site{i}.example" for i in range(2000)], spool=tmp_path)
    line = (tmp_path / "kosherd.jsonl").read_text().splitlines()[0]
    assert len(line) <= activity.MAX_LINE
    assert json.loads(line)["method"] == "SetWhitelist"


def test_day_start_is_local_midnight():
    now = time.time()
    start = activity.day_start(now)
    local = time.localtime(start)
    assert (local.tm_hour, local.tm_min, local.tm_sec) == (0, 0, 0)
    assert 0 <= now - start < 86400 + 3600  # a DST day is an hour longer


# -- the daemon's side ---------------------------------------------------------

def _daemon(users, tmp_path, monkeypatch):
    from kosherd import activity as activity_mod
    from kosherd.daemon import Daemon
    from kosherd.policy import Policy

    monkeypatch.setattr(activity_mod, "SPOOL_DIR", tmp_path)
    daemon = Daemon.__new__(Daemon)
    daemon.policy = Policy(revision=1, users=users)
    daemon._save_and_apply = lambda: None
    return daemon


def test_allowing_a_blocked_page_adds_an_allow_rule_ahead(tmp_path, monkeypatch):
    from kosherd.policy import UserPolicy

    user = UserPolicy(uid=1001, username="yosef", mode="filtered",
                      rules=[{"action": "block", "pattern": "*"}])
    daemon = _daemon([user], tmp_path, monkeypatch)
    daemon.impl_AllowUrl(1001, "https://example.com/needed/page", False, "")
    assert user.rules[0] == {"action": "allow", "pattern": "example.com/needed/page"}


def test_allowing_a_whole_site_for_a_whitelist_account_adds_the_domain(tmp_path, monkeypatch):
    from kosherd.policy import UserPolicy

    user = UserPolicy(uid=1001, username="shmuli", mode="whitelist")
    daemon = _daemon([user], tmp_path, monkeypatch)
    daemon.impl_AllowUrl(1001, "https://chabad.org/x", True, "")
    assert user.whitelist == ["chabad.org"]


def test_allowing_for_an_unmanaged_account_is_refused(tmp_path, monkeypatch):
    from kosherd.policy import PolicyError

    daemon = _daemon([], tmp_path, monkeypatch)
    with pytest.raises(PolicyError):
        daemon.impl_AllowUrl(4242, "https://example.com/", False, "")


def test_the_feed_names_the_people_in_it(tmp_path, monkeypatch):
    from kosherd.policy import UserPolicy

    daemon = _daemon([UserPolicy(uid=1001, username="yosef", mode="filtered"),
                      UserPolicy(uid=1000, username="avi", mode="filtered", admin=True)],
                     tmp_path, monkeypatch)
    activity.record("proxy", activity.BLOCK, 1001, url="https://a/",
                    why="category:video", spool=tmp_path)
    activity.record("kosherd", activity.CHANGE, 1001, by=1000,
                    method="SetFilterMode", args=[1001, "filtered"], spool=tmp_path)
    activity.record("proxy", activity.BLOCK, 7777, url="https://gone/", spool=tmp_path)

    found = json.loads(daemon.impl_ListActivity(0, -1).unpack()[0])
    by_url = {d.get("url"): d for d in found}
    assert by_url["https://a/"]["username"] == "yosef"
    assert by_url["https://gone/"]["username"] == "?"
    change = next(d for d in found if d["kind"] == "change")
    assert change["by_username"] == "avi"

    only = json.loads(daemon.impl_ListActivity(0, 1001).unpack()[0])
    assert all(d["uid"] == 1001 for d in only) and len(only) == 2


def test_the_summary_is_keyed_by_uid_for_the_cards(tmp_path, monkeypatch):
    from kosherd.policy import UserPolicy

    daemon = _daemon([UserPolicy(uid=1001, username="yosef", mode="filtered")],
                     tmp_path, monkeypatch)
    activity.record("proxy", activity.BLOCK, 1001, url="https://a/", spool=tmp_path)
    activity.record("proxy", activity.PICTURES, 1001, url="https://b/", spool=tmp_path)
    counts = json.loads(daemon.impl_ActivitySummary().unpack()[0])
    assert counts["1001"]["blocked"] == 1
    assert counts["1001"]["pictures"] == 1


def test_a_change_is_written_with_who_made_it_and_no_password(tmp_path, monkeypatch):
    from kosherd import access
    from kosherd.policy import UserPolicy

    daemon = _daemon([UserPolicy(uid=1001, username="yosef", mode="filtered")],
                     tmp_path, monkeypatch)
    daemon.policy.guardian_enabled = True
    daemon._note_change("SetFilterMode", [1001, "dnsfilter", "s3cret"], by=1000)
    daemon._note_change("SetGuardianPassword", ["old", "new"], by=1000)
    daemon._note_change("SetAdBlock", [False, "s3cret"], by=1000)

    found = activity.events(spool=tmp_path)
    text = json.dumps(found)
    assert "s3cret" not in text and "old" not in text
    mode = next(d for d in found if d["method"] == "SetFilterMode")
    assert mode["uid"] == 1001 and mode["by"] == 1000
    assert mode["args"] == [1001, "dnsfilter"]
    assert mode["guardian"] is True
    adblock = next(d for d in found if d["method"] == "SetAdBlock")
    assert adblock["uid"] == -1          # machine-wide, not about one account
    assert adblock["args"] == [False]
    assert all(m in access.ACTIONS for m in access.CHANGES)
