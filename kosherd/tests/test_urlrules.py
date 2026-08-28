import pytest

from kosherd.urlrules import ALLOW, BLOCK, Rule, RuleError, decide, parse_rules


def rules(*pairs):
    return [Rule(action=a, pattern=p) for a, p in pairs]


def test_bare_host_covers_whole_site():
    r = rules((BLOCK, "example.com"))
    assert decide(r, "https://example.com/")[0] == BLOCK
    assert decide(r, "https://example.com/deep/page?q=1")[0] == BLOCK
    assert decide(r, "https://other.com/")[0] == ALLOW


def test_path_prefix_wildcard():
    r = rules((BLOCK, "youtube.com/watch*"))
    assert decide(r, "https://youtube.com/watch?v=abc")[0] == BLOCK
    assert decide(r, "https://youtube.com/watchlist")[0] == BLOCK
    assert decide(r, "https://youtube.com/about")[0] == ALLOW


def test_directory_wildcard_matches_directory_itself():
    r = rules((BLOCK, "example.com/videos/*"))
    assert decide(r, "https://example.com/videos")[0] == BLOCK
    assert decide(r, "https://example.com/videos/")[0] == BLOCK
    assert decide(r, "https://example.com/videos/clip.mp4")[0] == BLOCK
    assert decide(r, "https://example.com/videosomething")[0] == ALLOW


def test_subdomain_wildcard_includes_base_domain():
    r = rules((BLOCK, "*.example.com"))
    assert decide(r, "https://example.com/")[0] == BLOCK
    assert decide(r, "https://cdn.example.com/x")[0] == BLOCK
    assert decide(r, "https://a.b.example.com/x")[0] == BLOCK
    assert decide(r, "https://notexample.com/")[0] == ALLOW


def test_www_is_ignored_on_both_sides():
    assert decide(rules((BLOCK, "example.com")), "https://www.example.com/")[0] == BLOCK
    assert decide(rules((BLOCK, "www.example.com")), "https://example.com/")[0] == BLOCK


def test_first_match_wins():
    r = rules((ALLOW, "example.com/safe/*"), (BLOCK, "example.com"))
    assert decide(r, "https://example.com/safe/page")[0] == ALLOW
    assert decide(r, "https://example.com/other")[0] == BLOCK


def test_deny_by_default_via_trailing_block_all():
    r = rules((ALLOW, "chinuch.org"), (BLOCK, "*"))
    assert decide(r, "https://chinuch.org/x")[0] == ALLOW
    assert decide(r, "https://anywhere.com/")[0] == BLOCK


def test_default_is_allow_when_nothing_matches():
    action, pattern = decide(rules((BLOCK, "bad.com")), "https://good.com/")
    assert action == ALLOW and pattern is None


def test_matched_pattern_is_reported():
    action, pattern = decide(rules((BLOCK, "youtube.com/watch*")),
                             "https://youtube.com/watch?v=1")
    assert action == BLOCK and pattern == "youtube.com/watch*"


def test_scheme_and_case_are_ignored():
    r = rules((BLOCK, "https://Example.COM/Path"))
    assert decide(r, "http://example.com/Path")[0] == BLOCK


def test_query_string_is_matchable():
    r = rules((BLOCK, "*/search?*safe=off*"))
    assert decide(r, "https://x.com/search?q=a&safe=off")[0] == BLOCK
    assert decide(r, "https://x.com/search?q=a&safe=on")[0] == ALLOW


def test_host_without_path_in_url():
    assert decide(rules((BLOCK, "example.com")), "example.com")[0] == BLOCK


def test_invalid_rules_rejected():
    with pytest.raises(RuleError):
        Rule(action="maybe", pattern="x.com")
    with pytest.raises(RuleError):
        Rule(action=BLOCK, pattern="   ")


def test_parse_rules_roundtrip():
    raw = [{"action": "block", "pattern": "a.com"}, {"action": "allow", "pattern": "b.com/x"}]
    parsed = parse_rules(raw)
    assert [r.to_dict() for r in parsed] == raw
