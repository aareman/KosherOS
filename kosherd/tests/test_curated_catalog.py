"""The catalogue must cover the mainstream sites a family actually reaches.

The University of Toulouse lists are deep but patchy on the obvious names:
thousands of sports blogs, but not espn.com. A curated supplement fills the
gap so the categories are complete out of the box, which is a product
requirement — a non-technical family should never have to build a list.
"""

import json
from pathlib import Path

ROOT = Path(__file__).parents[2]
CURATED = json.loads((ROOT / "scripts/curated-sites.json").read_text())


def sites(category: str) -> set[str]:
    return {s.split("/", 1)[0].lower() for s in CURATED[category]}


def test_the_big_sports_sites_are_covered():
    # The exact miss the first family test found: espn allowed under a
    # "sports" block because UT1 does not list it.
    for host in ("espn.com", "nba.com", "nfl.com", "skysports.com",
                 "bleacherreport.com", "cbssports.com", "foxsports.com"):
        assert host in sites("sports"), f"{host} missing from curated sports"


def test_the_big_gambling_sites_are_covered():
    for host in ("888.com", "bet365.com", "draftkings.com", "fanduel.com",
                 "betmgm.com", "pokerstars.com"):
        assert host in sites("gambling")


def test_the_big_dating_and_social_sites_are_covered():
    assert {"tinder.com", "bumble.com", "hinge.co"} <= sites("dating")
    assert {"facebook.com", "instagram.com", "tiktok.com", "reddit.com"} <= sites("social")


def test_every_curated_entry_is_a_bare_host_in_a_known_category():
    from kosherd.categories import CATEGORY_LABELS

    for category, entries in CURATED.items():
        if category.startswith("_"):
            continue
        assert category in CATEGORY_LABELS, f"unknown category {category!r}"
        for entry in entries:
            host = entry.split("/", 1)[0]
            assert "." in host and " " not in host and host == host.lower()


def test_the_builder_merges_curated_rows(tmp_path, monkeypatch):
    # curated_rows() returns (host, category) pairs the build step inserts.
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "fetchcat", ROOT / "scripts/fetch-categories.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    rows = mod.curated_rows(None)
    assert ("espn.com", "sports") in rows
    assert ("888.com", "gambling") in rows
    # A path is stripped to the host.
    assert all("/" not in h for h, _ in rows)
    # Filtering by category works, for a partial rebuild.
    only_sports = mod.curated_rows({"sports"})
    assert only_sports and all(c == "sports" for _, c in only_sports)


def test_the_source_meta_insert_is_a_parameter_tuple():
    # A dropped trailing comma made this a bare string once, the INSERT
    # raised, and the catalogue shipped with empty meta (kosherd logged
    # "version 0"). Keep both meta inserts binding a tuple.
    import re

    src = (ROOT / "scripts/fetch-categories.py").read_text()
    for key in ("version", "source"):
        m = re.search(rf"INSERT OR REPLACE INTO meta VALUES \('{key}', \?\)\",\s*\((.*?)\)\)",
                      src, re.S)
        assert m, f"could not find the {key} meta insert"
        assert m.group(1).rstrip().endswith(","), \
            f"the {key} meta insert must bind a tuple (trailing comma)"
