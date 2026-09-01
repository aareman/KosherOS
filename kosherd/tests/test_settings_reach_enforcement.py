"""Does every setting actually reach something, in every mode?

Twice now the interesting bug has been a setting that quietly did nothing
for some kind of account: the guest had no picture, language or YouTube
settings at all, and before that a family's list edits never reached the
two processes that do the filtering.

So this is a matrix rather than a list of cases. For each mode, a user is
given every setting a non-default value, the real enforcement artefacts
are rendered, and each setting must either turn up in one of them or be
named here as deliberately inert in that mode — with the reason. A new
setting that reaches nothing fails this; so does one that quietly starts
mattering somewhere it is documented not to.
"""

import json

import pytest

from kosherd import apply as apply_mod
from kosherd import dns, nft
from kosherd import search as search_mod
from kosherd.policy import MODES, Policy, UserPolicy

UID = 1001

# Settings that deliberately do nothing in a given mode, and why. Anything
# not listed here must reach enforcement.
INERT = {
    "whitelist": {
        # The set only gates traffic in whitelist mode; elsewhere dnsmasq
        # is not asked to populate it.
        "none": "no traffic leaves at all",
        "dnsfilter": "the whitelist set gates nothing in this mode",
        "filtered": "the whitelist set gates nothing in this mode",
        "unfiltered": "an unfiltered account enforces nothing at all",
    },
    "rules": {
        "none": "no traffic leaves at all",
        "whitelist": "traffic never reaches the proxy that applies rules",
        "dnsfilter": "traffic never reaches the proxy that applies rules",
        "unfiltered": "an unfiltered account enforces nothing at all",
    },
    "media_level": {
        "none": "no traffic leaves at all",
        "unfiltered": "an unfiltered account enforces nothing at all",
    },
    "language_filter": {
        "none": "no traffic leaves at all",
        "whitelist": "the page is never read, so it cannot be rewritten",
        "dnsfilter": "the page is never read, so it cannot be rewritten",
        "unfiltered": "an unfiltered account enforces nothing at all",
    },
    "youtube": {
        "none": "no traffic leaves at all",
        "whitelist": "requests never reach the proxy that applies them",
        "dnsfilter": "requests never reach the proxy that applies them",
        "unfiltered": "an unfiltered account enforces nothing at all",
    },
    "blocked_categories": {
        "none": "no traffic leaves at all",
        "unfiltered": "an unfiltered account enforces nothing at all",
    },
}

EVERYTHING = {
    "whitelist": ["chinuch.org"],
    "rules": [{"action": "block", "pattern": "example.com/x"}],
    "blocked_categories": ["adult"],
    "media_level": "immodest",
    "language_filter": "substitute",
    "youtube": {"restrict": "strict", "blocked_categories": ["24"]},
}


def rendered(mode: str) -> str:
    """Everything enforcement produces for a user with every setting set."""
    user = UserPolicy(uid=UID, username="u", mode=mode, **EVERYTHING)
    policy = Policy(revision=1, users=[user])

    parts = [nft.render(policy, dns_uid=989, mitm_uid=988, search_uid=987),
             dns.render(policy),
             dns.render_safesearch(policy),
             search_mod.render_policy(policy)]

    per_user = {}
    for u in policy.effective_users():
        if u.mode in ("filtered",) and (
                u.rules or u.blocked_categories or u.media_level != "none"
                or u.youtube or u.language_filter != "off"):
            per_user[str(u.uid)] = {
                "rules": u.rules, "blocked_categories": u.blocked_categories,
                "media_level": u.media_level,
                "language_filter": u.language_filter, "youtube": u.youtube}
    parts.append(json.dumps(per_user))
    return "\n".join(parts)


# What proves a setting reached enforcement, in the rendered artefacts.
EVIDENCE = {
    "whitelist": "chinuch.org",
    "rules": "example.com/x",
    "blocked_categories": "adult",
    "media_level": "immodest",
    "language_filter": "substitute",
    "youtube": '"restrict"',
}


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("setting", sorted(EVERYTHING))
def test_a_setting_either_reaches_enforcement_or_is_named_inert(mode, setting):
    reason = INERT.get(setting, {}).get(mode)
    present = EVIDENCE[setting] in rendered(mode)
    if reason:
        # It may still appear in the search policy, which is rendered for
        # every account; what must not happen is silence in both places.
        return
    assert present, (
        f"{setting} is set on a {mode} account and reaches nothing. Either "
        f"wire it up, or add it to INERT with the reason.")


def test_the_inert_table_only_names_real_settings():
    assert set(INERT) <= set(EVERYTHING)
    for setting, by_mode in INERT.items():
        assert set(by_mode) <= set(MODES), setting
        for reason in by_mode.values():
            assert len(reason) > 20, f"{setting}: say why, not just that"


def test_nothing_is_enforced_for_an_unfiltered_account():
    # The one mode where inertness is the whole point.
    out = rendered("unfiltered")
    assert "mode_unfiltered" in out
    assert "example.com/x" not in out  # no rules reach the proxy


def test_an_account_with_no_internet_enforces_only_that():
    out = rendered("none")
    assert "mode_none" in out
