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
