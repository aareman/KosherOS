"""Where an admin- or portal-supplied list is looked for.

The point of this module is a bug that had shipped: overrides lived under
/var/lib/kosher, which is 0700 because it holds the policy, so the proxy
and the search service — both unprivileged, and the only two things that
actually filter — could not read any of them. They fell back to the
shipped copy and said nothing, so the documented behaviour did not happen.
"""

from pathlib import Path

from kosherd import categories, content, language, lists, search, siterules


def test_an_override_is_looked_for_before_the_shipped_copy():
    found = lists.paths("wordlist.json")
    assert found[0] == lists.OVERRIDE_DIR / "wordlist.json"
    assert found[1] == lists.SHIPPED_DIR / "wordlist.json"


def test_every_list_uses_the_readable_override_directory():
    # A list whose override lands somewhere the services cannot read is a
    # setting that silently does nothing.
    for name, paths in [
        ("wordlist", language.WORDLIST_PATHS),
        ("content terms", content.TERMS_PATHS),
        ("site rules", siterules.RULES_PATHS),
        ("search blocklist", search.SEARCH_BLOCKLIST_PATHS),
    ]:
        assert paths[0].parent == lists.OVERRIDE_DIR, name
        assert paths[1].parent == lists.SHIPPED_DIR, name


def test_no_list_override_lives_under_the_private_state_directory():
    private = Path("/var/lib/kosher")
    everywhere = [*language.WORDLIST_PATHS, *content.TERMS_PATHS,
                  *siterules.RULES_PATHS, *search.SEARCH_BLOCKLIST_PATHS,
                  categories.LOCAL_BUNDLE_DIR]
    for path in everywhere:
        assert private not in path.parents, f"{path} is unreadable to the filter"


def test_an_override_actually_wins(tmp_path):
    shipped = tmp_path / "shipped.json"
    override = tmp_path / "override.json"
    shipped.write_text('{"replacements": {"a": "x", "b": "y"}}')
    override.write_text('{"replacements": {"c": "z"}}')
    assert len(language.load(override, shipped)) == 1
    assert len(language.load(shipped)) == 2


def test_a_missing_override_falls_through_to_the_shipped_copy(tmp_path):
    shipped = tmp_path / "shipped.json"
    shipped.write_text('{"replacements": {"a": "x"}}')
    assert len(language.load(tmp_path / "nope.json", shipped)) == 1
