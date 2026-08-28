"""The device must accept only genuine, newer portal policies."""

import base64
import json

import pytest

crypto = pytest.importorskip("cryptography", reason="cryptography not installed")
from cryptography.hazmat.primitives.asymmetric.ed25519 import (  # noqa: E402
    Ed25519PrivateKey,
)

from kosherd.sync import (  # noqa: E402
    Enrolment,
    SyncError,
    canonical_payload,
    verify_envelope,
)


@pytest.fixture
def portal_key():
    return Ed25519PrivateKey.generate()


def pubkey_b64(private) -> str:
    from cryptography.hazmat.primitives import serialization

    raw = private.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw)
    return base64.b64encode(raw).decode()


def envelope(private, policy: dict) -> dict:
    signature = private.sign(canonical_payload(policy))
    return {"policy": policy, "signature": base64.b64encode(signature).decode()}


def a_policy(revision: int = 5) -> dict:
    return {
        "schema_version": 1, "revision": revision, "source": "portal",
        "users": [{"uid": 1001, "username": "kid", "mode": "whitelist"}],
        "guardian": {"enabled": False},
    }


def test_accepts_a_genuine_newer_policy(portal_key):
    doc = verify_envelope(envelope(portal_key, a_policy(5)),
                          pubkey_b64(portal_key), current_revision=4)
    assert doc["revision"] == 5


def test_rejects_a_forged_signature(portal_key):
    other = Ed25519PrivateKey.generate()
    with pytest.raises(SyncError, match="signature"):
        verify_envelope(envelope(other, a_policy()), pubkey_b64(portal_key),
                        current_revision=0)


def test_rejects_tampering_after_signing(portal_key):
    env = envelope(portal_key, a_policy())
    env["policy"]["users"][0]["mode"] = "dnsfilter"  # loosen it in transit
    with pytest.raises(SyncError, match="signature"):
        verify_envelope(env, pubkey_b64(portal_key), current_revision=0)


def test_rejects_replay_of_an_older_policy(portal_key):
    with pytest.raises(SyncError, match="not newer"):
        verify_envelope(envelope(portal_key, a_policy(3)), pubkey_b64(portal_key),
                        current_revision=7)


def test_rejects_the_same_revision_twice(portal_key):
    with pytest.raises(SyncError, match="not newer"):
        verify_envelope(envelope(portal_key, a_policy(5)), pubkey_b64(portal_key),
                        current_revision=5)


def test_rejects_missing_fields(portal_key):
    with pytest.raises(SyncError, match="signature"):
        verify_envelope({"policy": a_policy()}, pubkey_b64(portal_key),
                        current_revision=0)
    with pytest.raises(SyncError, match="policy"):
        verify_envelope({"signature": "x"}, pubkey_b64(portal_key), current_revision=0)


def test_rejects_policy_without_revision(portal_key):
    policy = a_policy()
    del policy["revision"]
    with pytest.raises(SyncError, match="revision"):
        verify_envelope(envelope(portal_key, policy), pubkey_b64(portal_key),
                        current_revision=0)


def test_rejects_malformed_signature_encoding(portal_key):
    env = envelope(portal_key, a_policy())
    env["signature"] = "not base64!!"
    with pytest.raises(SyncError, match="malformed"):
        verify_envelope(env, pubkey_b64(portal_key), current_revision=0)


def test_canonical_payload_is_key_order_independent():
    assert canonical_payload({"a": 1, "b": 2}) == canonical_payload({"b": 2, "a": 1})


def test_enrolment_roundtrip(tmp_path):
    path = tmp_path / "enrolment.json"
    e = Enrolment("https://portal.example", "dev-1", "tok", "key")
    e.save(path)
    assert (path.stat().st_mode & 0o777) == 0o600
    assert Enrolment.load(path) == e


def test_enrolment_absent_is_not_an_error(tmp_path):
    assert Enrolment.load(tmp_path / "nope.json") is None


def test_enrolment_missing_field(tmp_path):
    path = tmp_path / "enrolment.json"
    path.write_text(json.dumps({"portal_url": "x"}))
    with pytest.raises(SyncError, match="missing"):
        Enrolment.load(path)
