"""Portal sync: verifying signed policy documents.

The portal is a *second* writer of the device policy, so the device must be
able to tell a genuine portal document from anything else. Every document
the portal sends is signed with an Ed25519 key whose public half is pinned
on the device at enrolment.

Only verification lives here, on purpose: the device never signs, never
holds the portal's private key, and a stolen device reveals nothing that
would let someone forge policy for another device.

Replay is prevented by the revision: a document is only accepted if its
revision is higher than the one already applied.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

ENROLMENT_PATH = Path("/var/lib/kosher/enrolment.json")


class SyncError(Exception):
    pass


@dataclass
class Enrolment:
    """What the device keeps after enrolling with a portal."""

    portal_url: str
    device_id: str
    device_token: str
    portal_public_key: str  # base64 Ed25519 public key

    def to_dict(self) -> dict:
        return {
            "portal_url": self.portal_url,
            "device_id": self.device_id,
            "device_token": self.device_token,
            "portal_public_key": self.portal_public_key,
        }

    @classmethod
    def from_dict(cls, doc: dict) -> "Enrolment":
        try:
            return cls(
                portal_url=doc["portal_url"],
                device_id=doc["device_id"],
                device_token=doc["device_token"],
                portal_public_key=doc["portal_public_key"],
            )
        except KeyError as e:
            raise SyncError(f"enrolment is missing {e}") from e

    @classmethod
    def load(cls, path: Path = ENROLMENT_PATH) -> "Enrolment | None":
        try:
            return cls.from_dict(json.loads(path.read_text()))
        except FileNotFoundError:
            return None
        except ValueError as e:
            raise SyncError(f"enrolment file is not valid JSON: {e}") from e

    def save(self, path: Path = ENROLMENT_PATH) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2) + "\n")
        path.chmod(0o600)


def canonical_payload(policy_doc: dict) -> bytes:
    """The exact bytes that get signed: stable key order, no stray spacing."""
    return json.dumps(policy_doc, sort_keys=True, separators=(",", ":")).encode()


def verify_envelope(envelope: dict, public_key_b64: str, *,
                    current_revision: int, field: str = "policy",
                    counter: str = "revision") -> dict:
    """Check a signed document and return the payload inside it.

    Raises SyncError unless the signature matches the pinned key and the
    counter moves forward (so an old document cannot be replayed).

    The same envelope carries policy and list updates. Lists are signed by
    the same key and replay-protected the same way, because a list is a
    filtering decision as surely as a policy is: rolling a family back to
    last year's word list is an attack, not a downgrade.
    """
    for required in (field, "signature"):
        if required not in envelope:
            raise SyncError(f"envelope is missing '{required}'")

    policy_doc = envelope[field]
    try:
        signature = base64.b64decode(envelope["signature"], validate=True)
        key = Ed25519PublicKey.from_public_bytes(
            base64.b64decode(public_key_b64, validate=True))
    except (ValueError, TypeError) as e:
        raise SyncError(f"malformed signature or key: {e}") from e

    try:
        key.verify(signature, canonical_payload(policy_doc))
    except InvalidSignature:
        raise SyncError("signature does not match the enrolled portal key") from None

    revision = policy_doc.get(counter)
    if not isinstance(revision, int):
        raise SyncError(f"document has no {counter}")
    if revision <= current_revision:
        raise SyncError(
            f"{counter} {revision} is not newer than {current_revision}")
    return policy_doc


# -- device-side client -------------------------------------------------------

class PortalClient:
    """Talks to the portal on the device's behalf.

    Deliberately outbound-only: the device polls, so no inbound port is ever
    opened on a family machine. urllib is used rather than a dependency —
    this runs inside kosherd, which we keep thin.
    """

    def __init__(self, enrolment: Enrolment, timeout: float = 30.0):
        self.enrolment = enrolment
        self.timeout = timeout

    def fetch_lists(self, current_version: int) -> dict | None:
        """A verified newer set of filter lists, or None if nothing is new.

        Separate from the policy because they change for different reasons
        and on different clocks: a policy changes when a parent changes
        their mind, a list changes when the web does.
        """
        return self._fetch("lists", "lists", "version", current_version)

    def fetch_policy(self, current_revision: int) -> dict | None:
        """Return a verified newer policy, or None when there is nothing new."""
        return self._fetch("policy", "policy", "revision", current_revision)

    def _fetch(self, endpoint: str, field: str, counter: str,
               current: int) -> dict | None:
        import urllib.error
        import urllib.request

        url = (f"{self.enrolment.portal_url.rstrip('/')}"
               f"/api/v1/devices/{self.enrolment.device_id}/{endpoint}")
        request = urllib.request.Request(
            url, headers={"X-Device-Token": self.enrolment.device_token})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                if response.status == 204:
                    return None
                envelope = json.loads(response.read())
        except urllib.error.HTTPError as e:
            if e.code in (204, 404):
                # 404 too: an older portal has no list endpoint, and that
                # is a portal to keep working with, not an error to raise.
                return None
            raise SyncError(f"portal returned HTTP {e.code}") from e
        except urllib.error.URLError as e:
            raise SyncError(f"cannot reach the portal: {e.reason}") from e
        except ValueError as e:
            raise SyncError(f"portal sent invalid JSON: {e}") from e

        try:
            return verify_envelope(envelope, self.enrolment.portal_public_key,
                                   current_revision=current, field=field,
                                   counter=counter)
        except SyncError as e:
            if "not newer" in str(e):
                return None  # already applied; not an error
            raise


def enrol(portal_url: str, code: str, timeout: float = 30.0) -> Enrolment:
    """Redeem an enrolment code and pin the portal's signing key."""
    import urllib.error
    import urllib.request

    url = f"{portal_url.rstrip('/')}/api/v1/enrol"
    body = json.dumps({"code": code}).encode()
    request = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            doc = json.loads(response.read())
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:200]
        raise SyncError(f"enrolment refused (HTTP {e.code}): {detail}") from e
    except urllib.error.URLError as e:
        raise SyncError(f"cannot reach the portal: {e.reason}") from e

    for field in ("device_id", "device_token", "portal_public_key"):
        if field not in doc:
            raise SyncError(f"portal response is missing '{field}'")
    return Enrolment(portal_url=portal_url, device_id=doc["device_id"],
                     device_token=doc["device_token"],
                     portal_public_key=doc["portal_public_key"])
