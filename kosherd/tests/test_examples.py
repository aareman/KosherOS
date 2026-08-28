"""Everything the repo ships as data must still be valid.

Cheap guards that catch a schema change (or a hand-edited example) before
it reaches a device.
"""

import json
from pathlib import Path

import pytest

from kosherd import dns, nft
from kosherd.policy import Policy

REPO = Path(__file__).parents[2]
EXAMPLES = sorted((REPO / "policy" / "examples").glob("*.json"))
BASELINE_NFT = REPO / "os-image" / "files" / "etc" / "kosher" / "nft" / "kosher.nft"


def test_there_are_examples():
    assert EXAMPLES, "policy/examples/ is empty"


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.name)
def test_example_validates_and_roundtrips(path):
    policy = Policy.from_dict(json.loads(path.read_text()))
    assert Policy.from_dict(policy.to_dict()).to_dict() == policy.to_dict()


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.name)
def test_example_renders_enforcement(path):
    policy = Policy.from_dict(json.loads(path.read_text()))
    ruleset = nft.render(policy, dns_uid=989, mitm_uid=988)
    assert "table inet kosher" in ruleset
    # Every managed user must be dispatched somewhere, or they would fall
    # through to the fail-closed default without anyone noticing.
    for user in policy.effective_users():
        assert f"{user.uid} : jump mode_" in ruleset
    dns.render(policy)


def test_the_schema_shipped_with_the_package_matches_the_source():
    source = (REPO / "policy" / "schema" / "policy.schema.json").read_text()
    bundled = (REPO / "kosherd" / "src" / "kosherd" / "data"
               / "policy.schema.json").read_text()
    assert json.loads(source) == json.loads(bundled), (
        "kosherd/src/kosherd/data/policy.schema.json is stale — copy the "
        "schema from policy/schema/ after changing it"
    )


def test_the_baseline_ruleset_is_fail_closed():
    # This file is what kosher-firewall.service loads before the network on
    # a machine that has never applied a policy.
    text = BASELINE_NFT.read_text()
    assert "table inet kosher" in text
    assert "jump mode_none" in text, "the baseline must deny unknown users"
    assert "vmap" not in text, "the shipped baseline should manage nobody yet"
