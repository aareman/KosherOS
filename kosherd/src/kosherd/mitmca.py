"""The inspection certificate authority.

Inspect mode can only read URLs by terminating TLS, which means the proxy
presents its own certificates. Those are only accepted if the machine
trusts the proxy's CA, so kosherd generates the CA once and installs it
into the system trust store.

This is the honest cost of URL-level filtering, and it is deliberately
visible: the CA lives on this machine only, is never shared between
installs, and only inspected users' traffic is decrypted.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)

MITM_DIR = Path("/var/lib/kosher/mitm")
CA_PEM = MITM_DIR / "mitmproxy-ca-cert.pem"
ANCHOR = Path("/etc/pki/ca-trust/source/anchors/kosheros-inspect-ca.crt")

# Firefox keeps its own trust store; this policy makes it honour the system
# one, so the CA does not have to be imported per profile.
FIREFOX_POLICY_DIR = Path("/etc/firefox/policies")
FIREFOX_POLICY = FIREFOX_POLICY_DIR / "policies.json"
FIREFOX_POLICY_JSON = """{
  "policies": {
    "Certificates": {
      "ImportEnterpriseRoots": true
    }
  }
}
"""


class CAError(Exception):
    pass


def ensure_ca() -> None:
    """Generate the CA if missing, then make the system trust it."""
    if not CA_PEM.exists():
        _generate()
    _install_trust()


def _generate() -> None:
    """Ask mitmproxy to create its CA (it does this on first start)."""
    MITM_DIR.mkdir(parents=True, exist_ok=True)
    res = subprocess.run(
        ["mitmdump", "--set", f"confdir={MITM_DIR}", "--version"],
        capture_output=True, text=True, timeout=60,
    )
    if not CA_PEM.exists():
        raise CAError(f"mitmproxy did not create a CA: {res.stderr.strip()[:200]}")
    # The proxy runs as kosher-mitm and needs to read its own key material.
    subprocess.run(["chown", "-R", "kosher-mitm:kosher-mitm", str(MITM_DIR)],
                   capture_output=True)
    log.info("generated inspection CA at %s", CA_PEM)


def _install_trust() -> None:
    anchor_text = CA_PEM.read_text()
    if not ANCHOR.exists() or ANCHOR.read_text() != anchor_text:
        ANCHOR.parent.mkdir(parents=True, exist_ok=True)
        ANCHOR.write_text(anchor_text)
        res = subprocess.run(["update-ca-trust"], capture_output=True, text=True)
        if res.returncode != 0:
            raise CAError(f"update-ca-trust failed: {res.stderr.strip()}")
        log.info("installed inspection CA into the system trust store")

    if not FIREFOX_POLICY.exists():
        FIREFOX_POLICY_DIR.mkdir(parents=True, exist_ok=True)
        FIREFOX_POLICY.write_text(FIREFOX_POLICY_JSON)
        log.info("enabled enterprise roots for Firefox")


def remove_trust() -> None:
    """Stop trusting the CA (inspect mode switched off everywhere)."""
    if ANCHOR.exists():
        ANCHOR.unlink()
        subprocess.run(["update-ca-trust"], capture_output=True)
        log.info("removed inspection CA from the system trust store")
