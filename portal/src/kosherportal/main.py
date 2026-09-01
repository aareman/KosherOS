"""KosherOS portal API.

Two audiences:

* **devices**, which enrol once with a code and then poll for signed policy
  documents. Device requests authenticate with a per-device token and are
  the only ones that receive signed envelopes;
* **the person configuring filters**, through a small admin API guarded by
  a shared admin token (single-tenant to begin with).

The device never trusts this server beyond the signature: policies are
signed with the portal's Ed25519 key, and the device checks that against
the key it pinned at enrolment (see kosherd/sync.py).
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from .store import Store, StoreError

DB_PATH = Path(os.environ.get("KOSHER_PORTAL_DB", "/var/lib/kosher-portal/portal.db"))
ADMIN_TOKEN = os.environ.get("KOSHER_PORTAL_ADMIN_TOKEN", "")

app = FastAPI(title="KosherOS portal", version="0.1.0")
store = Store(DB_PATH)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# -- auth ---------------------------------------------------------------------

def require_admin(authorization: str = Header(default="")) -> None:
    if not ADMIN_TOKEN:
        raise HTTPException(503, "portal has no admin token configured")
    expected = f"Bearer {ADMIN_TOKEN}"
    if not authorization or authorization != expected:
        raise HTTPException(401, "admin authentication required")


def require_device(
    device_id: str,
    x_device_token: str = Header(default=""),
) -> str:
    if not store.authenticate(device_id, x_device_token):
        raise HTTPException(401, "device authentication failed")
    return device_id


# -- models -------------------------------------------------------------------

class EnrolRequest(BaseModel):
    code: str = Field(min_length=1)


class EnrolResponse(BaseModel):
    device_id: str
    device_token: str
    portal_public_key: str


class NewDeviceRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)


class PolicyRequest(BaseModel):
    policy: dict


class ListsRequest(BaseModel):
    """A complete set of filter lists, published to every enrolled device."""

    lists: dict


# -- device endpoints ---------------------------------------------------------

@app.post("/api/v1/enrol", response_model=EnrolResponse)
def enrol(request: EnrolRequest) -> EnrolResponse:
    """Redeem a one-time code. This is how a device learns the portal key."""
    try:
        device_id, token = store.redeem_enrolment_code(request.code)
    except StoreError as e:
        raise HTTPException(400, str(e)) from e
    return EnrolResponse(device_id=device_id, device_token=token,
                         portal_public_key=store.public_key_b64())


@app.get("/api/v1/devices/{device_id}/policy")
def get_policy(device_id: str = Depends(require_device)) -> dict:
    """The device's current policy, signed. 204 when nothing is set yet."""
    store.touch(device_id, _now())
    device = store.get_device(device_id)
    if device is None or device.policy is None:
        raise HTTPException(204, "no policy set for this device")
    return store.sign(device.policy)


# -- admin endpoints ----------------------------------------------------------

@app.get("/api/v1/devices/{device_id}/lists")
def get_lists(device_id: str = Depends(require_device)) -> dict:
    """The signed filter lists, for any enrolled device.

    Same bundle for everyone: a word list is upstream's business, not a
    family's. What a family changes stays on their own machine and is
    never sent here — the portal has no reason to know it and no business
    holding it.
    """
    store.touch(device_id, _now())
    bundle = store.signed_lists()
    if bundle is None:
        raise HTTPException(204, "no filter lists published yet")
    return bundle


@app.put("/api/v1/admin/lists", dependencies=[Depends(require_admin)])
def set_lists(request: ListsRequest) -> dict:
    bundle = store.set_lists(request.lists)
    return {"version": bundle["version"], "lists": sorted(request.lists)}


@app.post("/api/v1/admin/devices", dependencies=[Depends(require_admin)])
def create_device(request: NewDeviceRequest) -> dict:
    code = store.create_enrolment_code(request.name)
    return {"name": request.name, "enrolment_code": code}


@app.get("/api/v1/admin/devices", dependencies=[Depends(require_admin)])
def list_devices() -> dict:
    return {"devices": [
        {"device_id": d.device_id, "name": d.name, "revision": d.revision,
         "last_seen": d.last_seen, "has_policy": d.policy is not None}
        for d in store.list_devices()
    ]}


@app.put("/api/v1/admin/devices/{device_id}/policy",
         dependencies=[Depends(require_admin)])
def set_policy(device_id: str, request: PolicyRequest) -> dict:
    try:
        policy = store.set_policy(device_id, request.policy)
    except StoreError as e:
        raise HTTPException(404, str(e)) from e
    return {"device_id": device_id, "revision": policy["revision"]}


@app.get("/api/v1/admin/public-key", dependencies=[Depends(require_admin)])
def public_key() -> dict:
    return {"portal_public_key": store.public_key_b64()}


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


def run() -> None:
    import uvicorn

    uvicorn.run(app, host=os.environ.get("KOSHER_PORTAL_HOST", "127.0.0.1"),
                port=int(os.environ.get("KOSHER_PORTAL_PORT", "8000")))
