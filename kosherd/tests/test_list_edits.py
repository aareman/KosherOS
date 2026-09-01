"""A family's edits to a list, and why they are a delta.

The lists ship complete on purpose. What a family will do is disagree with
a handful of entries — a word they want cleaned that is not listed, a term
that keeps blocking something innocent. If that edit were a copy of the
whole list, they would silently stop receiving every later improvement to
it, and nobody would notice for a year.
"""

import json

import pytest

from kosherd import content, language, lists, search


@pytest.fixture
def shipped(tmp_path, monkeypatch):
    """Point the list lookup at a temporary pair of directories."""
    ship = tmp_path / "shipped"
    over = tmp_path / "override"
    ship.mkdir()
    over.mkdir()
    monkeypatch.setattr(lists, "SHIPPED_DIR", ship)
    monkeypatch.setattr(lists, "OVERRIDE_DIR", over)
    return ship, over


def test_an_added_word_joins_the_shipped_ones(shipped):
    ship, over = shipped
    (ship / "wordlist.json").write_text(
        json.dumps({"replacements": {"blast": "bother", "drat": "dear"}}))
    lists.save_delta("wordlist.json", add={"phooey": "goodness"})
    words = language.load()
    assert len(words) == 3
    assert words.replacements["phooey"] == "goodness"
    assert words.replacements["blast"] == "bother"


def test_a_removed_word_goes_without_taking_the_others(shipped):
    ship, over = shipped
    (ship / "wordlist.json").write_text(
        json.dumps({"replacements": {"blast": "bother", "drat": "dear"}}))
    lists.save_delta("wordlist.json", remove=["drat"])
    words = language.load()
    assert "drat" not in words.replacements
    assert "blast" in words.replacements


def test_later_improvements_still_arrive(shipped):
    # The whole reason this is a delta. A family that added one word must
    # keep receiving everything added to the shipped list afterwards.
    ship, over = shipped
    (ship / "wordlist.json").write_text(
        json.dumps({"replacements": {"blast": "bother"}}))
    lists.save_delta("wordlist.json", add={"phooey": "goodness"})
    assert len(language.load()) == 2

    (ship / "wordlist.json").write_text(json.dumps({"replacements": {
        "blast": "bother", "drat": "dear", "botheration": "bother"}}))
    words = language.load()
    assert len(words) == 4
    assert "phooey" in words.replacements  # the family's edit survived
    assert "drat" in words.replacements    # and the new shipped words came


def test_a_whole_document_override_is_still_honoured(shipped):
    # The portal may legitimately ship a complete replacement.
    ship, over = shipped
    (ship / "wordlist.json").write_text(
        json.dumps({"replacements": {"blast": "bother"}}))
    (over / "wordlist.json").write_text(
        json.dumps({"replacements": {"only": "one"}}))
    words = language.load()
    assert list(words.replacements) == ["only"]


def test_search_terms_can_be_added_and_removed(shipped):
    ship, over = shipped
    (ship / "search-blocklist.json").write_text(
        json.dumps({"terms": ["porn", "xxx"]}))
    lists.save_delta("search-blocklist.json", add=["something else"],
                     remove=["xxx"])
    blocklist = search.load_blocklist()
    assert blocklist.contains_any("porn")
    assert blocklist.contains_any("something else")
    assert not blocklist.contains_any("xxx")


def test_a_content_term_can_be_added_at_a_level(shipped):
    ship, over = shipped
    (ship / "content-terms.json").write_text(json.dumps(
        {"terms": {"nsfw": {"25": ["porn"]}}}))
    lists.save_delta("content-terms.json",
                     add={"immodest": {"12": ["a local thing"]}})
    scorer = content.load()
    assert scorer.score("porn").level == content.NSFW
    assert "a local thing" in scorer.terms


def test_a_content_term_is_removed_wherever_it_sits(shipped):
    # A family taking a word out should not have to know which level it
    # was on, or at what weight.
    ship, over = shipped
    (ship / "content-terms.json").write_text(json.dumps(
        {"terms": {"nsfw": {"25": ["porn"]},
                   "immodest": {"5": ["bikini"], "3": ["sex"]}}}))
    lists.save_delta("content-terms.json", remove=["bikini", "sex"])
    scorer = content.load()
    assert "bikini" not in scorer.terms and "sex" not in scorer.terms
    assert "porn" in scorer.terms


def test_no_override_means_exactly_what_ships(shipped):
    ship, _ = shipped
    (ship / "wordlist.json").write_text(
        json.dumps({"replacements": {"blast": "bother"}}))
    assert len(language.load()) == 1


def test_an_unreadable_override_does_not_take_the_list_down(shipped):
    ship, over = shipped
    (ship / "wordlist.json").write_text(
        json.dumps({"replacements": {"blast": "bother"}}))
    (over / "wordlist.json").write_text("{ not json")
    assert len(language.load()) == 1


def test_a_saved_delta_is_readable_by_the_unprivileged_services(shipped):
    _, over = shipped
    lists.save_delta("wordlist.json", add={"x": "y"})
    mode = (over / "wordlist.json").stat().st_mode & 0o777
    assert mode & 0o044, f"mode {mode:o} is not readable by the filter"


# -- what the daemon will accept ----------------------------------------------

def _daemon(monkeypatch):
    from kosherd.daemon import Daemon

    daemon = Daemon.__new__(Daemon)
    daemon._save_and_apply = lambda: None
    return daemon


def test_only_the_family_facing_lists_can_be_edited(monkeypatch, shipped):
    from kosherd.daemon import PolicyError

    daemon = _daemon(monkeypatch)
    # site-rules and the category database are structured data and a
    # 5-million-row database; neither is a thing to hand a text box for.
    for name in ("site-rules.json", "categories.sqlite", "../policy.json"):
        with pytest.raises(PolicyError):
            daemon.impl_EditList(name, "{}", [], "")


def test_an_edit_that_is_not_a_handful_is_refused(monkeypatch, shipped):
    from kosherd.daemon import Daemon, PolicyError

    daemon = _daemon(monkeypatch)
    many = {f"word{i}": "x" for i in range(Daemon.MAX_EDITS + 1)}
    with pytest.raises(PolicyError) as caught:
        daemon.impl_EditList("wordlist.json", json.dumps(many), [], "")
    # The message says why, because the answer is not "try again with
    # fewer" — it is that the shipped list should be fixed for everybody.
    assert "not finished" in str(caught.value)


def test_malformed_additions_are_refused(monkeypatch, shipped):
    from kosherd.daemon import PolicyError

    daemon = _daemon(monkeypatch)
    with pytest.raises(PolicyError):
        daemon.impl_EditList("wordlist.json", "{not json", [], "")
    with pytest.raises(PolicyError):
        daemon.impl_EditList("wordlist.json", '"a string"', [], "")


def test_an_edit_round_trips(monkeypatch, shipped):
    ship, _ = shipped
    (ship / "wordlist.json").write_text(
        json.dumps({"replacements": {"blast": "bother"}}))
    daemon = _daemon(monkeypatch)
    daemon.impl_EditList("wordlist.json", json.dumps({"phooey": "goodness"}),
                         ["blast"], "")
    read = json.loads(daemon.impl_GetListEdits("wordlist.json").unpack()[0])
    assert read["add"] == {"phooey": "goodness"}
    assert read["remove"] == ["blast"]
    assert read["shipped"] == 1
    words = language.load()
    assert "phooey" in words.replacements and "blast" not in words.replacements


def test_the_shipped_count_is_reported_so_the_scale_is_visible(monkeypatch, shipped):
    # "119 ship with KosherOS, you have added two" is the sentence that
    # stops somebody trying to build the list themselves.
    ship, _ = shipped
    (ship / "search-blocklist.json").write_text(
        json.dumps({"terms": ["a", "b", "c"]}))
    daemon = _daemon(monkeypatch)
    read = json.loads(daemon.impl_GetListEdits("search-blocklist.json").unpack()[0])
    assert read["shipped"] == 3
