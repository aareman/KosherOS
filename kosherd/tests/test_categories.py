"""Category matching: the basis of content filtering."""

import json

import pytest

from kosherd.categories import Bundle, CategoryError, load, parse


def bundle(**by_category) -> Bundle:
    return parse({"version": "test", "domains": by_category})


def test_exact_domain_matches():
    b = bundle(adult=["bad.com"], gambling=["bets.com"])
    assert b.categories_of("bad.com") == {"adult"}
    assert b.categories_of("bets.com") == {"gambling"}
    assert b.categories_of("good.com") == set()


def test_subdomains_inherit_the_parent_entry():
    # Listing every subdomain is hopeless, so a parent entry must cover them.
    b = bundle(adult=["bad.com"])
    assert b.categories_of("cdn.bad.com") == {"adult"}
    assert b.categories_of("a.b.c.bad.com") == {"adult"}


def test_a_longer_entry_wins_over_its_parent():
    # A specific subdomain can be classified differently from its parent.
    b = bundle(video=["example.com"], adult=["nsfw.example.com"])
    assert b.categories_of("nsfw.example.com") == {"adult"}
    assert b.categories_of("www.example.com") == {"video"}


def test_a_domain_can_hold_several_categories():
    b = parse({"domains": {"social": ["x.com"], "video": ["x.com"]}})
    assert b.categories_of("x.com") == {"social", "video"}


def test_matching_ignores_case_and_trailing_dots():
    b = bundle(adult=["Bad.COM"])
    assert b.categories_of("WWW.BAD.com.") == {"adult"}


def test_a_similar_name_is_not_a_match():
    b = bundle(adult=["bad.com"])
    assert b.categories_of("notbad.com") == set()
    assert b.categories_of("bad.com.evil.net") == set()


def test_blocked_categories_of_reports_the_overlap():
    b = bundle(adult=["bad.com"], social=["chat.com"])
    assert b.blocked_categories_of("bad.com", ["adult", "gambling"]) == {"adult"}
    assert b.blocked_categories_of("chat.com", ["adult"]) == set()


def test_empty_and_missing_hosts_are_not_matched():
    b = bundle(adult=["bad.com"])
    assert b.categories_of("") == set()
    assert b.categories_of(None) == set()


def test_an_empty_bundle_blocks_nothing():
    assert Bundle().categories_of("anything.com") == set()


def test_bad_bundles_are_rejected():
    with pytest.raises(CategoryError):
        parse({"domains": {"adult": "not-a-list"}})
    with pytest.raises(CategoryError):
        parse("not an object")


def test_a_runtime_bundle_wins_over_the_shipped_one(tmp_path):
    shipped, runtime = tmp_path / "shipped", tmp_path / "runtime"
    for directory, version in ((shipped, "1"), (runtime, "2")):
        directory.mkdir()
        (directory / "categories.json").write_text(json.dumps(
            {"version": version, "domains": {"adult": ["bad.com"]}}))
    assert load(runtime, shipped).version == "2"


def test_a_broken_bundle_falls_back_instead_of_failing(tmp_path):
    broken, good = tmp_path / "broken", tmp_path / "good"
    broken.mkdir(); good.mkdir()
    (broken / "categories.json").write_text("{ not json")
    (good / "categories.json").write_text(json.dumps(
        {"version": "9", "domains": {"adult": ["bad.com"]}}))
    assert load(broken, good).version == "9"


def test_no_bundle_at_all_is_survivable(tmp_path):
    assert len(load(tmp_path)) == 0


def test_the_shipped_seed_bundle_parses():
    from pathlib import Path

    seed = (Path(__file__).parents[2] / "os-image" / "files" / "usr" / "share"
            / "kosher" / "categories" / "categories.json")
    b = parse(json.loads(seed.read_text()))
    assert len(b) > 0
    assert "adult" in b.categories


# -- the on-disk database (what actually ships) -------------------------------

def build_db(path, rows, version="test"):
    import sqlite3

    db = sqlite3.connect(path)
    db.executescript(
        "CREATE TABLE domains (domain TEXT, category TEXT, "
        "PRIMARY KEY (domain, category)) WITHOUT ROWID;"
        "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);")
    db.executemany("INSERT INTO domains VALUES (?, ?)", rows)
    db.execute("INSERT INTO meta VALUES ('version', ?)", (version,))
    db.execute("INSERT INTO meta VALUES ('source', 'test')")
    db.commit()
    db.close()
    return path


def test_the_database_matches_like_the_json_bundle(tmp_path):
    from kosherd.categories import SqliteBundle

    build_db(tmp_path / "categories.sqlite",
             [("bad.com", "adult"), ("chat.com", "social"),
              ("nsfw.example.com", "adult"), ("example.com", "video")])
    bundle = SqliteBundle(tmp_path / "categories.sqlite")
    assert bundle.categories_of("bad.com") == {"adult"}
    assert bundle.categories_of("cdn.bad.com") == {"adult"}       # subdomains
    assert bundle.categories_of("nsfw.example.com") == {"adult"}  # longest wins
    assert bundle.categories_of("www.example.com") == {"video"}
    assert bundle.categories_of("unlisted.org") == set()
    assert len(bundle) == 4
    assert bundle.categories == {"adult", "social", "video"}


def test_the_database_is_preferred_over_json(tmp_path):
    import json as _json

    from kosherd.categories import load_any

    build_db(tmp_path / "categories.sqlite", [("bad.com", "adult")], version="db")
    (tmp_path / "categories.json").write_text(_json.dumps(
        {"version": "json", "domains": {"adult": ["other.com"]}}))
    assert load_any(tmp_path).version == "db"


def test_json_is_still_used_when_no_database_exists(tmp_path):
    import json as _json

    from kosherd.categories import load_any

    (tmp_path / "categories.json").write_text(_json.dumps(
        {"version": "json-only", "domains": {"adult": ["bad.com"]}}))
    assert load_any(tmp_path).version == "json-only"


def test_a_corrupt_database_falls_back_rather_than_failing(tmp_path):
    import json as _json

    from kosherd.categories import load_any

    (tmp_path / "categories.sqlite").write_bytes(b"not a database")
    (tmp_path / "categories.json").write_text(_json.dumps(
        {"version": "fallback", "domains": {"adult": ["bad.com"]}}))
    assert load_any(tmp_path).version == "fallback"
