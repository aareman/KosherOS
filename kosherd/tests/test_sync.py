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


# -- list bundles over the same signed channel --------------------------------

def list_envelope(private, bundle: dict) -> dict:
    signature = private.sign(canonical_payload(bundle))
    return {"lists": bundle, "signature": base64.b64encode(signature).decode()}


def test_a_list_bundle_verifies_like_a_policy(portal_key):
    # Same key, same envelope, same replay rule. A list is a filtering
    # decision as surely as a policy is.
    bundle = {"version": 3, "lists": {"wordlist.json": {"replacements": {}}}}
    verified = verify_envelope(list_envelope(portal_key, bundle),
                               pubkey_b64(portal_key), current_revision=2,
                               field="lists", counter="version")
    assert verified["version"] == 3


def test_a_replayed_list_bundle_is_refused(portal_key):
    # Rolling a family back to last year's word list is an attack.
    bundle = {"version": 3, "lists": {}}
    with pytest.raises(SyncError):
        verify_envelope(list_envelope(portal_key, bundle),
                        pubkey_b64(portal_key), current_revision=3,
                        field="lists", counter="version")


def test_a_list_bundle_signed_by_the_wrong_key_is_refused(portal_key):
    other = Ed25519PrivateKey.generate()
    bundle = {"version": 1, "lists": {}}
    with pytest.raises(SyncError):
        verify_envelope(list_envelope(portal_key, bundle),
                        pubkey_b64(other), current_revision=0,
                        field="lists", counter="version")


def test_a_bundle_with_no_version_is_refused(portal_key):
    bundle = {"lists": {}}
    with pytest.raises(SyncError):
        verify_envelope(list_envelope(portal_key, bundle),
                        pubkey_b64(portal_key), current_revision=0,
                        field="lists", counter="version")


def test_an_older_portal_without_a_list_endpoint_is_not_an_error(monkeypatch):
    # A portal that predates lists must keep working, not start failing
    # every sync with an error nobody can act on.
    import urllib.error
    import urllib.request

    from kosherd.sync import PortalClient

    def not_found(request, timeout=None):
        raise urllib.error.HTTPError(request.full_url, 404, "Not Found",
                                     {}, None)

    monkeypatch.setattr(urllib.request, "urlopen", not_found)
    client = PortalClient(Enrolment(portal_url="https://portal.example",
                                    device_id="d", device_token="t",
                                    portal_public_key="x"))
    assert client.fetch_lists(0) is None
