"""Talking to the portal over HTTP.

test_sync.py covers the crypto and revision rules; this covers the two
urllib functions around them, which were the only part of sync.py left
uncovered. No server is needed — urlopen is faked, so the tests assert the
handling (204 both ways, an already-applied revision, unreachable portals,
malformed responses, missing enrolment fields) rather than the transport.
"""

import base64
import io
import json
import urllib.error

import pytest

pytest.importorskip("cryptography", reason="cryptography not installed")
from cryptography.hazmat.primitives.asymmetric.ed25519 import (  # noqa: E402
    Ed25519PrivateKey,
)

from kosherd import sync  # noqa: E402
from kosherd.sync import (  # noqa: E402
    Enrolment,
    PortalClient,
    SyncError,
    canonical_payload,
    enrol,
)


# -- helpers (mirroring test_sync.py so both files read on their own) --------

@pytest.fixture
def portal_key():
    return Ed25519PrivateKey.generate()


def pubkey_b64(private) -> str:
    from cryptography.hazmat.primitives import serialization

    raw = private.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw)
    return base64.b64encode(raw).decode()


def a_policy(revision: int = 5) -> dict:
    return {
        "schema_version": 1, "revision": revision, "source": "portal",
        "users": [{"uid": 1001, "username": "kid", "mode": "whitelist"}],
        "guardian": {"enabled": False},
    }


def signed(private, policy: dict) -> dict:
    return {"policy": policy,
            "signature": base64.b64encode(
                private.sign(canonical_payload(policy))).decode()}


class FakeResponse(io.BytesIO):
    def __init__(self, body: bytes, status: int = 200):
        super().__init__(body)
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def fake_urlopen(monkeypatch, result):
    """Point urllib at `result`: a FakeResponse, or an exception to raise."""
    seen = {}

    def opener(request, timeout=None):
        seen["url"] = request.full_url
        seen["headers"] = dict(request.header_items())
        seen["body"] = request.data
        seen["timeout"] = timeout
        if isinstance(result, Exception):
            raise result
        return result

    import urllib.request

    monkeypatch.setattr(urllib.request, "urlopen", opener)
    return seen


def an_enrolment(private) -> Enrolment:
    return Enrolment(portal_url="https://portal.example.org/",
                     device_id="dev-1", device_token="tok-1",
                     portal_public_key=pubkey_b64(private))


# -- fetching a policy -------------------------------------------------------

def test_fetches_and_verifies_a_newer_policy(portal_key, monkeypatch):
    body = json.dumps(signed(portal_key, a_policy(9))).encode()
    seen = fake_urlopen(monkeypatch, FakeResponse(body))
    client = PortalClient(an_enrolment(portal_key))

    policy = client.fetch_policy(current_revision=5)

    assert policy["revision"] == 9
    # The device asks for its own policy and identifies itself by token.
    assert seen["url"] == "https://portal.example.org/api/v1/devices/dev-1/policy"
    assert seen["headers"].get("X-device-token") == "tok-1"


def test_a_trailing_slash_does_not_double_up(portal_key, monkeypatch):
    body = json.dumps(signed(portal_key, a_policy(9))).encode()
    seen = fake_urlopen(monkeypatch, FakeResponse(body))
    PortalClient(an_enrolment(portal_key)).fetch_policy(current_revision=1)
    assert "//api" not in seen["url"]


def test_no_content_means_nothing_new(portal_key, monkeypatch):
    fake_urlopen(monkeypatch, FakeResponse(b"", status=204))
    assert PortalClient(an_enrolment(portal_key)).fetch_policy(5) is None


def test_no_content_reported_as_an_error_also_means_nothing_new(portal_key, monkeypatch):
    # Some servers surface 204 through HTTPError rather than a response.
    fake_urlopen(monkeypatch, urllib.error.HTTPError(
        "u", 204, "No Content", {}, None))
    assert PortalClient(an_enrolment(portal_key)).fetch_policy(5) is None


def test_an_already_applied_revision_is_not_an_error(portal_key, monkeypatch):
    # The sync timer runs unattended, so "nothing newer" must be quiet
    # rather than a failure the admin has to see.
    body = json.dumps(signed(portal_key, a_policy(5))).encode()
    fake_urlopen(monkeypatch, FakeResponse(body))
    assert PortalClient(an_enrolment(portal_key)).fetch_policy(5) is None


def test_a_forged_policy_still_raises(portal_key, monkeypatch):
    # Anything other than "not newer" must surface, not be swallowed.
    other = Ed25519PrivateKey.generate()
    body = json.dumps(signed(other, a_policy(9))).encode()
    fake_urlopen(monkeypatch, FakeResponse(body))
    with pytest.raises(SyncError):
        PortalClient(an_enrolment(portal_key)).fetch_policy(5)


def test_an_http_error_is_reported(portal_key, monkeypatch):
    fake_urlopen(monkeypatch, urllib.error.HTTPError("u", 503, "boom", {}, None))
    with pytest.raises(SyncError, match="HTTP 503"):
        PortalClient(an_enrolment(portal_key)).fetch_policy(5)


def test_an_unreachable_portal_is_reported(portal_key, monkeypatch):
    fake_urlopen(monkeypatch, urllib.error.URLError("no route to host"))
    with pytest.raises(SyncError, match="cannot reach the portal"):
        PortalClient(an_enrolment(portal_key)).fetch_policy(5)


def test_garbage_from_the_portal_is_reported(portal_key, monkeypatch):
    fake_urlopen(monkeypatch, FakeResponse(b"<html>not json</html>"))
    with pytest.raises(SyncError, match="invalid JSON"):
        PortalClient(an_enrolment(portal_key)).fetch_policy(5)


def test_the_request_carries_a_timeout(portal_key, monkeypatch):
    # A hung portal must not wedge the daemon.
    body = json.dumps(signed(portal_key, a_policy(9))).encode()
    seen = fake_urlopen(monkeypatch, FakeResponse(body))
    PortalClient(an_enrolment(portal_key), timeout=7).fetch_policy(1)
    assert seen["timeout"] == 7


# -- enrolling ---------------------------------------------------------------

def test_enrolment_redeems_a_code_and_pins_the_key(monkeypatch):
    doc = {"device_id": "dev-9", "device_token": "tok-9",
           "portal_public_key": "AAAA"}
    seen = fake_urlopen(monkeypatch, FakeResponse(json.dumps(doc).encode()))

    enrolment = enrol("https://portal.example.org/", "CODE-123")

    assert enrolment.device_id == "dev-9"
    assert enrolment.portal_public_key == "AAAA"
    assert seen["url"] == "https://portal.example.org/api/v1/enrol"
    assert json.loads(seen["body"]) == {"code": "CODE-123"}


@pytest.mark.parametrize("missing", ["device_id", "device_token",
                                     "portal_public_key"])
def test_an_incomplete_enrolment_response_is_refused(monkeypatch, missing):
    # Without the pinned key a device would accept any policy at all.
    doc = {"device_id": "d", "device_token": "t", "portal_public_key": "k"}
    del doc[missing]
    fake_urlopen(monkeypatch, FakeResponse(json.dumps(doc).encode()))
    with pytest.raises(SyncError, match=missing):
        enrol("https://portal.example.org", "CODE")


def test_a_refused_code_reports_the_portal_message(monkeypatch):
    error = urllib.error.HTTPError(
        "u", 403, "Forbidden", {}, io.BytesIO(b"code already redeemed"))
    fake_urlopen(monkeypatch, error)
    with pytest.raises(SyncError, match="already redeemed"):
        enrol("https://portal.example.org", "CODE")


def test_enrolling_against_an_unreachable_portal_is_reported(monkeypatch):
    fake_urlopen(monkeypatch, urllib.error.URLError("name not resolved"))
    with pytest.raises(SyncError, match="cannot reach the portal"):
        enrol("https://portal.example.org", "CODE")


# -- the enrolment file ------------------------------------------------------

def test_the_enrolment_file_is_root_only(tmp_path, portal_key):
    # It holds the device token: readable by anyone would let a user talk to
    # the portal as this machine.
    path = tmp_path / "enrolment.json"
    an_enrolment(portal_key).save(path)
    assert (path.stat().st_mode & 0o777) == 0o600


def test_a_corrupt_enrolment_file_is_reported(tmp_path):
    path = tmp_path / "enrolment.json"
    path.write_text("{not json")
    with pytest.raises(SyncError, match="not valid JSON"):
        Enrolment.load(path)


def test_sync_module_needs_no_gobject():
    # kosher-sync runs on a timer inside kosherd; keeping this import-light
    # means the sync path cannot be broken by desktop typelibs.
    assert "gi" not in getattr(sync, "__dict__", {})
