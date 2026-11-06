"""The Usage page's numbers: counted by the writers, folded by kosherd,
and kept to a fixed size however long the computer runs."""

from __future__ import annotations

import datetime
import json
import os
import time

from kosherd import usage

NOON = time.mktime((2026, 10, 20, 12, 0, 0, 0, 0, -1))


def _day(back: int) -> str:
    return (datetime.date.fromtimestamp(NOON) - datetime.timedelta(days=back)).isoformat()


def _line(spool, day, uid, writer="proxy", **numbers):
    with open(spool / f"{writer}{usage.SUFFIX}", "a") as handle:
        handle.write(json.dumps({"day": day, "uid": uid, **numbers}) + "\n")


def test_a_tally_writes_one_line_per_account_and_day(tmp_path):
    tally = usage.Tally("proxy", spool=tmp_path)
    tally.add(1002, requests=3, bytes_in=1000, blocked=1)
    tally.add(1003, requests=1)
    tally.add(None, requests=99)  # not a filtered account's connection
    tally.latency(1002, 0.004)
    tally.latency(1002, 0.300)
    assert tally.flush()

    lines = [json.loads(x) for x in (tmp_path / "proxy.usage").read_text().splitlines()]
    assert sorted(d["uid"] for d in lines) == [1002, 1003]
    yosef = next(d for d in lines if d["uid"] == 1002)
    assert yosef["requests"] == 3 and yosef["blocked"] == 1
    assert sum(yosef["lat"]) == 2 and yosef["lat_ms"] == 304
    assert "url" not in yosef and "bytes_out" not in yosef  # zeros are not written

    # Flushed means gone from memory: the next flush writes nothing new.
    tally.flush()
    assert len((tmp_path / "proxy.usage").read_text().splitlines()) == 2


def test_a_full_spool_keeps_the_counts_in_memory(tmp_path, monkeypatch):
    monkeypatch.setattr(usage, "MAX_SPOOL_BYTES", 10)
    tally = usage.Tally("proxy", spool=tmp_path)
    tally.add(1002, requests=5)
    assert not tally.flush()
    tally.add(1002, requests=2)
    monkeypatch.setattr(usage, "MAX_SPOOL_BYTES", 10_000)
    assert tally.flush()
    lines = (tmp_path / "proxy.usage").read_text().splitlines()
    assert [json.loads(x)["requests"] for x in lines] == [7]


def test_fold_moves_the_spool_into_the_store_and_empties_it(tmp_path):
    spool, store_path = tmp_path / "spool", tmp_path / "usage.json"
    spool.mkdir()
    _line(spool, _day(0), 1002, requests=10, blocked=2)
    _line(spool, _day(0), 1002, requests=5)
    _line(spool, _day(3), 1002, requests=7)
    _line(spool, _day(0), 1003, writer="search", searches=1)
    (spool / "proxy.jsonl").write_text("{}\n")  # the activity log is not ours

    assert usage.fold(spool=spool, path=store_path, now=NOON) == 4
    assert sorted(p.name for p in spool.iterdir()) == ["proxy.jsonl"]
    store = usage.load(store_path)
    assert store["days"][_day(0)]["1002"]["requests"] == 15
    assert store["total"]["1002"]["requests"] == 22
    assert store["since"] == _day(3)
    assert oct(os.stat(store_path).st_mode & 0o777) == "0o600"

    # Folding again adds only what is new.
    _line(spool, _day(0), 1002, requests=1)
    usage.fold(spool=spool, path=store_path, now=NOON)
    assert usage.load(store_path)["total"]["1002"]["requests"] == 23


def test_a_fold_interrupted_last_time_is_finished(tmp_path):
    spool, store_path = tmp_path / "spool", tmp_path / "usage.json"
    spool.mkdir()
    _line(spool, _day(0), 1002, requests=4)
    os.rename(spool / "proxy.usage", spool / "proxy.usage.folding")
    _line(spool, _day(0), 1002, requests=1)  # the proxy started a new file
    usage.fold(spool=spool, path=store_path, now=NOON)
    usage.fold(spool=spool, path=store_path, now=NOON)
    assert usage.load(store_path)["total"]["1002"]["requests"] == 5
    assert list(spool.iterdir()) == []


def test_bad_lines_are_skipped_not_fatal(tmp_path):
    spool, store_path = tmp_path / "spool", tmp_path / "usage.json"
    spool.mkdir()
    with open(spool / "proxy.usage", "w") as handle:
        handle.write("not json\n")
        handle.write(json.dumps({"day": "yesterday", "uid": 1002, "requests": 1}) + "\n")
        handle.write(json.dumps({"day": _day(0), "uid": "1002", "requests": 1}) + "\n")
        handle.write(json.dumps({"day": _day(0), "uid": 1002, "requests": -5,
                                 "blocked": True, "lat": [1, 2]}) + "\n")
        handle.write(json.dumps({"day": _day(0), "uid": 1002, "requests": 2}) + "\n")
    usage.fold(spool=spool, path=store_path, now=NOON)
    total = usage.load(store_path)["total"]["1002"]
    assert total["requests"] == 2 and total["blocked"] == 0
    assert total["lat"] == [0] * usage.BUCKETS


def test_old_days_age_out_but_stay_in_all_time(tmp_path):
    """The point of the store: its size does not grow with time."""
    spool, store_path = tmp_path / "spool", tmp_path / "usage.json"
    spool.mkdir()
    for back in range(usage.KEEP_DAYS + 40):
        _line(spool, _day(back), 1002, requests=1)
    usage.fold(spool=spool, path=store_path, now=NOON)
    store = usage.load(store_path)
    assert len(store["days"]) == usage.KEEP_DAYS
    assert store["total"]["1002"]["requests"] == usage.KEEP_DAYS + 40

    # And a day passing with nothing new still prunes.
    later = NOON + 10 * 86400
    usage.fold(spool=spool, path=store_path, now=later)
    assert len(usage.load(store_path)["days"]) == usage.KEEP_DAYS - 10


def test_report_adds_up_each_range(tmp_path):
    store = {"days": {}, "total": {}}
    for back, requests in ((0, 10), (1, 20), (6, 40), (7, 80), (29, 160), (45, 320)):
        entry = usage.empty()
        entry["requests"] = requests
        store["days"].setdefault(_day(back), {})["1002"] = entry
    other = usage.empty()
    other["requests"] = 1
    store["days"][_day(0)]["1003"] = other
    store["total"] = {"1002": {"requests": 1000}, "1003": {"requests": 1}}

    out = usage.report(store, now=NOON)
    assert out["today"]["people"]["1002"]["requests"] == 10
    assert out["today"]["everyone"]["requests"] == 11
    assert out["week"]["people"]["1002"]["requests"] == 70
    assert out["month"]["people"]["1002"]["requests"] == 310
    assert out["all"]["people"]["1002"]["requests"] == 1000
    assert "1003" not in out["week"]["people"] or \
        out["week"]["people"]["1003"]["requests"] == 1


def test_filter_time_reads_as_average_median_and_95th():
    entry = usage.empty()
    for ms in [1] * 50 + [8] * 45 + [400] * 5:
        entry["lat"][usage.bucket_of(ms)] += 1
        entry["lat_ms"] += ms
    numbers = usage.summarise(entry)
    assert numbers["timed"] == 100
    assert numbers["p50_ms"] == 1
    assert numbers["p95_ms"] == 10
    assert numbers["avg_ms"] == round((50 + 360 + 2000) / 100, 1)
    assert usage.summarise(usage.empty())["p50_ms"] is None
    slow = usage.empty()
    slow["lat"][-1] = 1
    assert usage.summarise(slow)["p95_ms"] == usage.SLOWEST_MS


def test_the_daemon_folds_before_it_answers(tmp_path, monkeypatch):
    from kosherd import daemon

    spool, store_path = tmp_path / "spool", tmp_path / "usage.json"
    spool.mkdir()
    monkeypatch.setattr(usage, "SPOOL_DIR", spool)
    monkeypatch.setattr(usage, "STORE_PATH", store_path)
    _line(spool, usage.day_key(), 1002, requests=3)

    handler = daemon.Daemon.__new__(daemon.Daemon)
    out = json.loads(handler.impl_Usage().unpack()[0])
    assert out["today"]["people"]["1002"]["requests"] == 3
    assert out["all"]["everyone"]["requests"] == 3


def test_the_demo_has_numbers_for_every_range():
    from kosherd.demo import DemoClient

    out = DemoClient().usage()
    for name in ("today", "week", "month", "all"):
        assert out[name]["everyone"]["requests"] > 0, name
    assert out["all"]["everyone"]["requests"] > out["month"]["everyone"]["requests"]
