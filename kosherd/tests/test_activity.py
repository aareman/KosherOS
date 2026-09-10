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
