"""Shared fixtures for kosherd's tests.

The policy schema under test is the one in this tree. kosherd prefers the
schema the OS installed at /usr/share/kosher/policy.schema.json, which on a
KosherOS developer machine is the RELEASED schema — so a change that adds a
field passed in CI and failed on the developer's own computer with
"Additional properties are not allowed", until the image caught up (#265).
Every test validates against the bundled copy instead.
"""

import pytest

from kosherd import policy as policy_mod


@pytest.fixture(autouse=True)
def _bundled_policy_schema(monkeypatch, tmp_path):
    monkeypatch.setattr(policy_mod, "SCHEMA_PATH", tmp_path / "no-installed-schema.json")
    monkeypatch.setattr(policy_mod, "_schema_cache", None)
    yield
    policy_mod._schema_cache = None
