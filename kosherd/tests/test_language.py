"""Cleaning up language without corrupting the page."""

import json
from pathlib import Path

from kosherd.language import Wordlist, load, word_pattern

WORDLIST = (Path(__file__).parents[2]
            / "os-image/files/usr/share/kosher/wordlist.json")


def wl() -> Wordlist:
    return Wordlist({"fuck": "freak", "shit": "junk", "damn": "darn"})


def test_a_listed_word_is_replaced():
    assert wl().clean("what the fuck")[0] == "what the freak"


def test_capitalisation_is_preserved():
    assert wl().clean("Fuck this")[0] == "Freak this"
    assert wl().clean("FUCK this")[0] == "FREAK this"
    assert wl().clean("fuck this")[0] == "freak this"


def test_disguised_spellings_are_caught():
    # The point of folding: someone writing f*ck meant the word.
    for spelling in ("f*ck", "f**k", "f#ck", "fu(k", "f.u.c.k", "F**K"):
        cleaned, count = wl().clean(f"oh {spelling} no")
        assert count == 1, spelling
        assert "freak" in cleaned.lower(), spelling


def test_ordinary_words_are_left_alone():
    # A list that rewrites innocent words gets the feature switched off.
    text = "The dam held. Class discussion about Scunthorpe and assessment."
    assert wl().clean(text) == (text, 0)


def test_substrings_of_longer_words_are_not_matched():
    # "damn" inside "damnation"? The whole token folds to damnation, which
    # is not listed, so it stays.
    assert wl().clean("eternal damnation")[0] == "eternal damnation"


def test_counts_are_reported():
    _, count = wl().clean("fuck the shit out of this damn thing")
    assert count == 3


def test_contains_any_for_block_mode():
    assert wl().contains_any("a damn shame") is True
    assert wl().contains_any("a fine day") is False


def test_an_empty_list_changes_nothing():
    assert Wordlist().clean("fuck")[0] == "fuck"
    assert Wordlist().contains_any("fuck") is False


def test_empty_and_missing_text():
    assert wl().clean("") == ("", 0)


def test_wildcards_stand_for_a_letter_not_nothing():
    # The bug this pins: folding used to DELETE *, so f*ck became fck and
    # matched nothing at all.
    assert Wordlist({"shit": "junk"}).clean("sh1t")[0] == "junk"
    assert Wordlist({"damn": "darn"}).clean("d*mn")[0] == "darn"
    assert "[" in word_pattern("fuck")  # each letter is a character class


def test_admin_list_beats_the_shipped_one(tmp_path):
    admin, shipped = tmp_path / "a.json", tmp_path / "s.json"
    admin.write_text(json.dumps({"replacements": {"foo": "bar"}}))
    shipped.write_text(json.dumps({"replacements": {"baz": "qux"}}))
    loaded = load(admin, shipped)
    assert loaded.clean("foo")[0] == "bar"


def test_a_broken_list_falls_back(tmp_path):
    broken, good = tmp_path / "b.json", tmp_path / "g.json"
    broken.write_text("{ not json")
    good.write_text(json.dumps({"replacements": {"foo": "bar"}}))
    assert load(broken, good).clean("foo")[0] == "bar"


def test_no_list_at_all_is_survivable(tmp_path):
    assert len(load(tmp_path / "nope.json")) == 0


def test_the_shipped_list_loads():
    from pathlib import Path

    path = (Path(__file__).parents[2] / "os-image" / "files" / "usr" / "share"
            / "kosher" / "wordlist.json")
    doc = json.loads(path.read_text())
    listed = Wordlist(doc["replacements"])
    assert len(listed) > 0
    assert listed.clean("fuck")[0] == "freak"


# -- cleaning HTML without corrupting it --------------------------------------

from kosherd.language import clean_html, html_contains_any  # noqa: E402


def test_visible_text_is_cleaned():
    html = "<p>what the fuck</p>"
    assert clean_html(html, wl())[0] == "<p>what the freak</p>"


def test_script_contents_are_never_touched():
    # Rewriting inside a script breaks the site in a way nobody can debug.
    html = '<script>var x = "fuck"; damn();</script><p>damn</p>'
    cleaned, count = clean_html(html, wl())
    assert '"fuck"' in cleaned and "damn();" in cleaned
    assert "<p>darn</p>" in cleaned
    assert count == 1


def test_attributes_and_urls_survive():
    html = '<a class="damn-btn" href="/shit/page" title="damn">damn</a>'
    cleaned, _ = clean_html(html, wl())
    assert 'class="damn-btn"' in cleaned
    assert 'href="/shit/page"' in cleaned
    assert 'title="damn"' in cleaned
    assert ">darn<" in cleaned


def test_style_blocks_and_comments_survive():
    html = "<style>.damn{color:red}</style><!-- damn --><p>damn</p>"
    cleaned, count = clean_html(html, wl())
    assert ".damn{color:red}" in cleaned
    assert "<!-- damn -->" in cleaned
    assert count == 1


def test_block_mode_sees_only_visible_text():
    assert html_contains_any('<p>a damn shame</p>', wl()) is True
    # A word that appears only in a script or an attribute is not on screen.
    assert html_contains_any('<script>var damn=1</script>', wl()) is False
    assert html_contains_any('<a title="damn">hello</a>', wl()) is False


def test_an_empty_list_leaves_html_identical():
    html = "<p>fuck</p>"
    assert clean_html(html, Wordlist()) == (html, 0)


def test_matching_a_page_does_not_cost_more_than_it_has_to():
    """A guard on the shape of the pattern, not on the clock.

    The matcher used one NAMED capture group per word, which made the
    engine track 145 sets of group boundaries through every character of
    every page: 40 ms on a 28 KB page against 4 ms for the identical
    pattern without them, paid whether or not anything matched. The word
    that hit is recovered afterwards instead, which costs something only
    when there is a hit.
    """
    wordlist = load(WORDLIST)
    assert len(wordlist) > 100, "needs the real list to be meaningful"
    for _presence, pattern in wordlist._patterns:
        assert pattern.groups == 1, (
            f"{pattern.groups} capture groups in a page-scanning pattern; "
            "it must stay at one")


def test_the_word_that_matched_is_still_identified(tmp_path):
    # Which is what the named groups were for, so it has to keep working
    # for every word and every disguise.
    wordlist = load(WORDLIST)
    for word in list(wordlist.replacements)[:20]:
        cleaned, count = wordlist.clean(f"you {word} thing")
        assert count == 1, word
        assert wordlist.replacements[word] in cleaned, word


def test_the_longest_word_still_wins():
    # "bullshit" must not be cleaned as "bull" + "shit".
    wordlist = Wordlist({"shit": "shoot", "bullshit": "nonsense"})
    assert wordlist.clean("that is bullshit")[0] == "that is nonsense"
