"""Portal storage and signing.

Single-tenant and deliberately small: devices, their current policy, and
the Ed25519 key the portal signs policies with. SQLite keeps deployment to
"run the container and mount a volume".

The private key never leaves the portal; devices only ever hold the public
half, pinned at enrolment.
"""

from __future__ import annotations

import base64
import json
import secrets
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS devices (
    device_id   TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    token       TEXT NOT NULL,
    policy      TEXT,
    revision    INTEGER NOT NULL DEFAULT 0,
    last_seen   TEXT
);
CREATE TABLE IF NOT EXISTS enrolment_codes (
    code        TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    used        INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS portal_key (
    id          INTEGER PRIMARY KEY CHECK (id = 1),
    private_key TEXT NOT NULL
);
-- One row per setting the portal holds for every device rather than for
-- one. So far: the filter list bundle, which is upstream's business and
-- not any family's.
CREATE TABLE IF NOT EXISTS settings (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL
);
"""


class StoreError(Exception):
    pass


@dataclass
class Device:
    device_id: str
    name: str
    revision: int
    last_seen: str | None
    policy: dict | None


class Store:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as db:
            db.executescript(SCHEMA)
            db.commit()
        self._ensure_key()

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        return db

    # -- signing key -------------------------------------------------------

    def _ensure_key(self) -> None:
        with closing(self._connect()) as db:
            row = db.execute("SELECT private_key FROM portal_key WHERE id = 1").fetchone()
            if row:
                return
            key = Ed25519PrivateKey.generate()
            raw = key.private_bytes(
                encoding=serialization.Encoding.Raw,
                format=serialization.PrivateFormat.Raw,
                encryption_algorithm=serialization.NoEncryption())
            db.execute("INSERT INTO portal_key (id, private_key) VALUES (1, ?)",
                       (base64.b64encode(raw).decode(),))
            db.commit()

    def private_key(self) -> Ed25519PrivateKey:
        with closing(self._connect()) as db:
            row = db.execute("SELECT private_key FROM portal_key WHERE id = 1").fetchone()
        return Ed25519PrivateKey.from_private_bytes(base64.b64decode(row["private_key"]))

    def public_key_b64(self) -> str:
        raw = self.private_key().public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw)
        return base64.b64encode(raw).decode()

    def _sign_payload(self, document: dict) -> str:
        """The signature over exactly the bytes a device will reconstruct."""
        payload = json.dumps(document, sort_keys=True,
                             separators=(",", ":")).encode()
        return base64.b64encode(self.private_key().sign(payload)).decode()

    def sign(self, policy: dict) -> dict:
        """Wrap a policy in the signed envelope devices verify."""
        return {"policy": policy, "signature": self._sign_payload(policy)}

    # -- enrolment ---------------------------------------------------------

    def create_enrolment_code(self, name: str) -> str:
        code = secrets.token_urlsafe(9)
        with closing(self._connect()) as db:
            db.execute("INSERT INTO enrolment_codes (code, name) VALUES (?, ?)",
                       (code, name))
            db.commit()
        return code

    def redeem_enrolment_code(self, code: str) -> tuple[str, str]:
        """Consume a one-time code; returns (device_id, device_token)."""
        with closing(self._connect()) as db:
            row = db.execute(
                "SELECT name, used FROM enrolment_codes WHERE code = ?", (code,)
            ).fetchone()
            if row is None:
                raise StoreError("unknown enrolment code")
            if row["used"]:
                raise StoreError("this enrolment code has already been used")
            device_id = secrets.token_urlsafe(9)
            token = secrets.token_urlsafe(32)
            db.execute("UPDATE enrolment_codes SET used = 1 WHERE code = ?", (code,))
            db.execute(
                "INSERT INTO devices (device_id, name, token) VALUES (?, ?, ?)",
                (device_id, row["name"], token))
            db.commit()
        return device_id, token

    # -- devices -----------------------------------------------------------

    def authenticate(self, device_id: str, token: str) -> bool:
        with closing(self._connect()) as db:
            row = db.execute("SELECT token FROM devices WHERE device_id = ?",
                             (device_id,)).fetchone()
        return row is not None and secrets.compare_digest(row["token"], token)

    def touch(self, device_id: str, when: str) -> None:
        with closing(self._connect()) as db:
            db.execute("UPDATE devices SET last_seen = ? WHERE device_id = ?",
                       (when, device_id))
            db.commit()

    def get_device(self, device_id: str) -> Device | None:
        with closing(self._connect()) as db:
            row = db.execute(
                "SELECT device_id, name, revision, last_seen, policy FROM devices "
                "WHERE device_id = ?", (device_id,)).fetchone()
        if row is None:
            return None
        return Device(row["device_id"], row["name"], row["revision"],
                      row["last_seen"], json.loads(row["policy"]) if row["policy"] else None)

    def list_devices(self) -> list[Device]:
        with closing(self._connect()) as db:
            rows = db.execute(
                "SELECT device_id, name, revision, last_seen, policy FROM devices "
                "ORDER BY name").fetchall()
        return [Device(r["device_id"], r["name"], r["revision"], r["last_seen"],
                       json.loads(r["policy"]) if r["policy"] else None) for r in rows]

    # -- filter lists -----------------------------------------------------
    #
    # One bundle for every device, not per device: a word list is not a
    # family's business, it is upstream's. What a family changes is their
    # own delta, which lives on the device and is never sent here.

    def set_lists(self, documents: dict) -> dict:
        """Publish a new set of filter lists, signed, with the next version."""
        version = self.lists_version() + 1
        bundle = {"version": version, "lists": documents}
        with closing(self._connect()) as db:
            db.execute(
                "INSERT INTO settings (key, value) VALUES ('lists', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (json.dumps(bundle),))
            db.commit()
        return bundle

    def lists_version(self) -> int:
        bundle = self.get_lists()
        return int(bundle.get("version", 0)) if bundle else 0

    def get_lists(self) -> dict | None:
        with closing(self._connect()) as db:
            row = db.execute(
                "SELECT value FROM settings WHERE key = 'lists'").fetchone()
        if not row:
            return None
        try:
            return json.loads(row[0])
        except ValueError:
            return None

    def signed_lists(self) -> dict | None:
        bundle = self.get_lists()
        if bundle is None:
            return None
        return {"lists": bundle,
                "signature": self._sign_payload(bundle)}

    def set_policy(self, device_id: str, policy: dict) -> dict:
        """Store a new policy for a device, bumping its revision."""
        device = self.get_device(device_id)
        if device is None:
            raise StoreError(f"unknown device {device_id}")
        policy = dict(policy)
        policy["revision"] = device.revision + 1
        policy["source"] = "portal"
        with closing(self._connect()) as db:
            db.execute("UPDATE devices SET policy = ?, revision = ? WHERE device_id = ?",
                       (json.dumps(policy), policy["revision"], device_id))
            db.commit()
        return policy
