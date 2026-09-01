"""Portal API: enrolment, signed policy delivery, and access control."""

import base64
import json
import os

import pytest

pytest.importorskip("fastapi", reason="fastapi not installed")
pytest.importorskip("cryptography", reason="cryptography not installed")

from cryptography.hazmat.primitives.asymmetric.ed25519 import (  # noqa: E402
    Ed25519PublicKey,
)
from fastapi.testclient import TestClient  # noqa: E402

ADMIN = "test-admin-token"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("KOSHER_PORTAL_DB", str(tmp_path / "portal.db"))
    monkeypatch.setenv("KOSHER_PORTAL_ADMIN_TOKEN", ADMIN)
    import importlib

    from kosherportal import main

    importlib.reload(main)
    return TestClient(main.app)


def admin_headers() -> dict:
    return {"Authorization": f"Bearer {ADMIN}"}


def a_policy(users=None) -> dict:
    return {
        "schema_version": 1, "revision": 0, "source": "portal",
        "users": users or [{"uid": 1001, "username": "kid", "mode": "whitelist"}],
        "guardian": {"enabled": False},
    }


def enrol_a_device(client, name="Family laptop"):
    created = client.post("/api/v1/admin/devices", json={"name": name},
                          headers=admin_headers()).json()
    enrolled = client.post("/api/v1/enrol", json={"code": created["enrolment_code"]})
    return enrolled.json()


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_enrolment_flow(client):
    device = enrol_a_device(client)
    assert device["device_id"] and device["device_token"]
    # the pinned key is a real Ed25519 public key
    Ed25519PublicKey.from_public_bytes(base64.b64decode(device["portal_public_key"]))


def test_enrolment_code_is_single_use(client):
    created = client.post("/api/v1/admin/devices", json={"name": "x"},
                          headers=admin_headers()).json()
    code = created["enrolment_code"]
    assert client.post("/api/v1/enrol", json={"code": code}).status_code == 200
    second = client.post("/api/v1/enrol", json={"code": code})
    assert second.status_code == 400 and "already been used" in second.text


def test_unknown_enrolment_code_refused(client):
    assert client.post("/api/v1/enrol", json={"code": "nope"}).status_code == 400


def test_policy_requires_the_device_token(client):
    device = enrol_a_device(client)
    url = f"/api/v1/devices/{device['device_id']}/policy"
    assert client.get(url).status_code == 401
    assert client.get(url, headers={"X-Device-Token": "wrong"}).status_code == 401


def test_one_device_cannot_read_another(client):
    first = enrol_a_device(client, "first")
    second = enrol_a_device(client, "second")
    response = client.get(f"/api/v1/devices/{second['device_id']}/policy",
                          headers={"X-Device-Token": first["device_token"]})
    assert response.status_code == 401


def test_policy_is_signed_and_verifies_on_the_device(client):
    from kosherd.sync import verify_envelope

    device = enrol_a_device(client)
    client.put(f"/api/v1/admin/devices/{device['device_id']}/policy",
               json={"policy": a_policy()}, headers=admin_headers())
    envelope = client.get(f"/api/v1/devices/{device['device_id']}/policy",
                          headers={"X-Device-Token": device["device_token"]}).json()
    policy = verify_envelope(envelope, device["portal_public_key"], current_revision=0)
    assert policy["users"][0]["username"] == "kid"
    assert policy["source"] == "portal"


def test_revision_increases_with_each_update(client):
    device = enrol_a_device(client)
    revisions = []
    for mode in ("whitelist", "dnsfilter", "none"):
        response = client.put(
            f"/api/v1/admin/devices/{device['device_id']}/policy",
            json={"policy": a_policy([{"uid": 1001, "username": "kid", "mode": mode}])},
            headers=admin_headers())
        revisions.append(response.json()["revision"])
    assert revisions == [1, 2, 3]


def test_admin_endpoints_need_the_admin_token(client):
    assert client.get("/api/v1/admin/devices").status_code == 401
    assert client.post("/api/v1/admin/devices", json={"name": "x"},
                       headers={"Authorization": "Bearer wrong"}).status_code == 401


def test_device_token_is_not_admin_access(client):
    device = enrol_a_device(client)
    response = client.get("/api/v1/admin/devices",
                          headers={"Authorization": f"Bearer {device['device_token']}"})
    assert response.status_code == 401


def test_listing_shows_enrolled_devices(client):
    enrol_a_device(client, "laptop")
    devices = client.get("/api/v1/admin/devices", headers=admin_headers()).json()["devices"]
    assert [d["name"] for d in devices] == ["laptop"]
    assert devices[0]["has_policy"] is False


def test_polling_records_last_seen(client):
    device = enrol_a_device(client)
    client.get(f"/api/v1/devices/{device['device_id']}/policy",
               headers={"X-Device-Token": device["device_token"]})
    devices = client.get("/api/v1/admin/devices", headers=admin_headers()).json()["devices"]
    assert devices[0]["last_seen"] is not None


# -- filter lists -------------------------------------------------------------

def test_lists_are_published_to_every_device(client):
    # One bundle for everyone: a word list is upstream's business, not a
    # family's, and the portal has no reason to hold what a family changed.
    body = {"lists": {"wordlist.json": {"replacements": {"blast": "bother"}}}}
    published = client.put("/api/v1/admin/lists", json=body,
                           headers=admin_headers())
    assert published.status_code == 200
    assert published.json()["version"] == 1

    for _ in range(2):
        device = enrol_a_device(client)
        got = client.get(f"/api/v1/devices/{device['device_id']}/lists",
                         headers={"X-Device-Token": device["device_token"]})
        assert got.status_code == 200
        assert got.json()["lists"]["lists"]["wordlist.json"] == \
            body["lists"]["wordlist.json"]


def test_the_list_version_moves_forward_on_every_publish(client):
    # It is what the device compares against, so a bundle that did not
    # advance is a bundle a device will refuse as a replay.
    seen = []
    for i in range(3):
        response = client.put(
            "/api/v1/admin/lists",
            json={"lists": {"wordlist.json": {"replacements": {f"w{i}": "x"}}}},
            headers=admin_headers())
        seen.append(response.json()["version"])
    assert seen == [1, 2, 3]


def test_lists_are_signed_with_the_portal_key(client):
    from kosherd.sync import verify_envelope

    client.put("/api/v1/admin/lists",
               json={"lists": {"wordlist.json": {"replacements": {"a": "b"}}}},
               headers=admin_headers())
    key = client.get("/api/v1/admin/public-key",
                     headers=admin_headers()).json()["portal_public_key"]
    device = enrol_a_device(client)
    envelope = client.get(f"/api/v1/devices/{device['device_id']}/lists",
                          headers={"X-Device-Token": device["device_token"]}).json()

    verified = verify_envelope(envelope, key, current_revision=0,
                               field="lists", counter="version")
    assert verified["lists"]["wordlist.json"] == {"replacements": {"a": "b"}}


def test_a_replayed_list_bundle_is_refused(client):
    # Rolling a family back to last year's word list is an attack, not a
    # downgrade, so the same replay rule applies as for the policy.
    from kosherd.sync import SyncError, verify_envelope

    client.put("/api/v1/admin/lists",
               json={"lists": {"wordlist.json": {"replacements": {"a": "b"}}}},
               headers=admin_headers())
    key = client.get("/api/v1/admin/public-key",
                     headers=admin_headers()).json()["portal_public_key"]
    device = enrol_a_device(client)
    envelope = client.get(f"/api/v1/devices/{device['device_id']}/lists",
                          headers={"X-Device-Token": device["device_token"]}).json()

    with pytest.raises(SyncError):
        verify_envelope(envelope, key, current_revision=1,
                        field="lists", counter="version")


def test_a_tampered_list_bundle_is_refused(client):
    from kosherd.sync import SyncError, verify_envelope

    client.put("/api/v1/admin/lists",
               json={"lists": {"wordlist.json": {"replacements": {"a": "b"}}}},
               headers=admin_headers())
    key = client.get("/api/v1/admin/public-key",
                     headers=admin_headers()).json()["portal_public_key"]
    device = enrol_a_device(client)
    envelope = client.get(f"/api/v1/devices/{device['device_id']}/lists",
                          headers={"X-Device-Token": device["device_token"]}).json()
    envelope["lists"]["lists"]["wordlist.json"] = {"replacements": {}}

    with pytest.raises(SyncError):
        verify_envelope(envelope, key, current_revision=0,
                        field="lists", counter="version")


def test_a_device_with_no_token_gets_no_lists(client):
    client.put("/api/v1/admin/lists",
               json={"lists": {"wordlist.json": {"replacements": {"a": "b"}}}},
               headers=admin_headers())
    device = enrol_a_device(client)
    assert client.get(f"/api/v1/devices/{device['device_id']}/lists").status_code == 401


def test_no_lists_published_yet_is_not_an_error(client):
    device = enrol_a_device(client)
    response = client.get(f"/api/v1/devices/{device['device_id']}/lists",
                          headers={"X-Device-Token": device["device_token"]})
    assert response.status_code == 204


def test_a_catalogue_manifest_rides_with_the_lists(client):
    # 190 MB of database cannot go in a JSON document, so the bundle
    # carries a URL and a hash and the device fetches the file. The
    # signature over the hash is what lets the download come from a CDN.
    manifest = {"version": 12, "url": "https://cdn.example/categories.sqlite",
                "sha256": "a" * 64, "size": 190_000_000}
    published = client.put("/api/v1/admin/lists",
                           json={"lists": {}, "catalog": manifest},
                           headers=admin_headers())
    assert published.status_code == 200

    device = enrol_a_device(client)
    envelope = client.get(f"/api/v1/devices/{device['device_id']}/lists",
                          headers={"X-Device-Token": device["device_token"]}).json()
    assert envelope["lists"]["catalog"] == manifest


def test_a_catalogue_manifest_without_a_hash_is_refused(client):
    # Publishing a URL with no hash would give devices something to
    # download and no way to know what it is.
    for manifest in ({"version": 1, "url": "https://cdn.example/c"},
                     {"version": 1, "sha256": "a" * 64},
                     {"url": "https://cdn.example/c", "sha256": "a" * 64}):
        response = client.put("/api/v1/admin/lists",
                              json={"lists": {}, "catalog": manifest},
                              headers=admin_headers())
        assert response.status_code == 400, manifest


def test_the_catalogue_manifest_is_covered_by_the_signature(client):
    from kosherd.sync import SyncError, verify_envelope

    manifest = {"version": 3, "url": "https://cdn.example/c",
                "sha256": "b" * 64}
    client.put("/api/v1/admin/lists", json={"lists": {}, "catalog": manifest},
               headers=admin_headers())
    key = client.get("/api/v1/admin/public-key",
                     headers=admin_headers()).json()["portal_public_key"]
    device = enrol_a_device(client)
    envelope = client.get(f"/api/v1/devices/{device['device_id']}/lists",
                          headers={"X-Device-Token": device["device_token"]}).json()
    envelope["lists"]["catalog"]["url"] = "https://attacker.example/c"
    with pytest.raises(SyncError):
        verify_envelope(envelope, key, current_revision=0,
                        field="lists", counter="version")


def test_the_publishing_script_produces_a_bundle_devices_accept(
        client, tmp_path, monkeypatch):
    """End to end: what the script sends is what a device installs.

    The device side was signed, verified and tested while nothing could
    actually put a bundle into the portal. A feature that only works if
    somebody hand-crafts JSON is a feature that stays switched off.
    """
    import importlib.util
    import sys
    from pathlib import Path

    from kosherd import lists
    from kosherd.sync import verify_envelope

    monkeypatch.setattr(lists, "PORTAL_DIR", tmp_path / "portal")
    monkeypatch.setattr(lists, "PORTAL_VERSION_PATH",
                        tmp_path / "portal" / "version.json")

    spec = importlib.util.spec_from_file_location(
        "publish_lists",
        Path(__file__).parents[2] / "scripts/publish-lists.py")
    publish = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(publish)

    # What the script would send, from this repository's own lists.
    body = {"lists": publish.read_lists()}
    assert set(body["lists"]) == set(publish.LISTS)

    response = client.put("/api/v1/admin/lists", json=body,
                          headers=admin_headers())
    assert response.status_code == 200

    key = client.get("/api/v1/admin/public-key",
                     headers=admin_headers()).json()["portal_public_key"]
    device = enrol_a_device(client)
    envelope = client.get(f"/api/v1/devices/{device['device_id']}/lists",
                          headers={"X-Device-Token": device["device_token"]}).json()
    bundle = verify_envelope(envelope, key, current_revision=0,
                             field="lists", counter="version")

    # And a device installs every one of them.
    installed = lists.install_portal_lists(bundle["lists"], bundle["version"])
    assert sorted(installed) == sorted(publish.LISTS)


def test_the_script_only_sends_names_a_device_will_accept():
    # Sending anything else is silently dropped on arrival, which is worse
    # than refusing to send it.
    import importlib.util
    from pathlib import Path

    from kosherd import lists

    spec = importlib.util.spec_from_file_location(
        "publish_lists",
        Path(__file__).parents[2] / "scripts/publish-lists.py")
    publish = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(publish)
    assert set(publish.LISTS) <= lists.ALLOWED_LISTS
