"""The inspection certificate authority.

Inspect mode can only read URLs by terminating TLS, which means the proxy
presents its own certificates. Those are only accepted if the machine
trusts the proxy's CA, so kosherd generates the CA once and installs it
into the system trust store.

This is the honest cost of URL-level filtering, and it is deliberately
visible: the CA lives on this machine only, is never shared between
installs, and only inspected users' traffic is decrypted.

Flatpak apps do not see the system trust store (#223). Inside the sandbox
/etc is the runtime's, so the bundle path every app reads holds the
runtime's own roots without the CA, and an app that checks certificates
itself (1Password, anything on OpenSSL or rustls) fails on a filtered
account. Flatpak will not share a path under /etc, so kosherd keeps a copy
of the system bundle in FLATPAK_CA_DIR and a system-wide override shares
it read-only with every app, installed now or later, and points the
certificate variables at it. A person's own override for an app still
wins over it.
"""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)

MITM_DIR = Path("/var/lib/kosher-mitm")
CA_PEM = MITM_DIR / "mitmproxy-ca-cert.pem"
ANCHOR = Path("/etc/pki/ca-trust/source/anchors/kosheros-inspect-ca.crt")

# The system bundle update-ca-trust writes, with the CA in it.
SYSTEM_BUNDLE = Path("/etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem")
# Its copy for Flatpak apps, and the override that hands it to them.
FLATPAK_CA_DIR = Path("/var/lib/kosher-ca")
FLATPAK_BUNDLE = FLATPAK_CA_DIR / "tls-ca-bundle.pem"
FLATPAK_OVERRIDE = Path("/var/lib/flatpak/overrides/global")
# The variables /etc/environment.d/50-kosher-ca.conf points at the system
# bundle. The session passes them into the sandbox, where that path is the
# runtime's file, so each is pointed at the copy instead.
FLATPAK_ENV = ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "PIP_CERT", "NODE_EXTRA_CA_CERTS",
               "CARGO_HTTP_CAINFO", "GIT_SSL_CAINFO", "NIX_SSL_CERT_FILE")

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


def installed() -> tuple[bool, str | None]:
    """Is inspection's certificate authority in place and trusted?

    Worth its own answer because the failure is both severe and confusing:
    without it, every HTTPS page a filtered account opens shows a
    certificate warning. The person sees a broken internet and a scary
    security error, and nothing connects that to the filter — so this gets
    reported in words rather than left in the journal.
    """
    if not CA_PEM.exists():
        return False, "the inspection certificate has not been generated"
    if not ANCHOR.exists():
        return False, ("the inspection certificate is not in this computer's "
                       "trust store, so secure sites will show a warning")
    try:
        if ANCHOR.read_bytes() != CA_PEM.read_bytes():
            return False, ("the trusted copy of the inspection certificate "
                           "does not match the one in use, so secure sites "
                           "will show a warning")
    except OSError as e:
        return False, f"the inspection certificate could not be read ({e})"
    problem = flatpak_problem()
    if problem:
        return False, problem
    return True, None


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

    _sync_flatpak_quietly()


def remove_trust() -> None:
    """Stop trusting the CA (inspect mode switched off everywhere)."""
    if ANCHOR.exists():
        ANCHOR.unlink()
        subprocess.run(["update-ca-trust"], capture_output=True)
        log.info("removed inspection CA from the system trust store")
    _sync_flatpak_quietly()


# -- Flatpak apps (#223) --------------------------------------------------------

def _write_if_changed(path: Path, data: bytes, mode: int) -> bool:
    try:
        if path.read_bytes() == data:
            return False
    except OSError:
        pass
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    os.chmod(tmp, mode)
    os.replace(tmp, path)
    return True


def render_override(existing: str) -> str:
    """The global override with the copy shared and the variables pointed
    at it. Everything else in the file is kept as it was: groups, keys,
    comments, and other entries in `filesystems`."""
    share = f"{FLATPAK_CA_DIR}:ro"
    wanted_env = {name: str(FLATPAK_BUNDLE) for name in FLATPAK_ENV}
    groups: list[tuple[str, list[str]]] = [("", [])]
    for line in existing.splitlines():
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            groups.append((stripped[1:-1], []))
        else:
            groups[-1][1].append(line)

    def group(name: str) -> list[str]:
        for gname, lines in groups:
            if gname == name:
                return lines
        lines: list[str] = []
        groups.append((name, lines))
        return lines

    context = group("Context")
    for i, line in enumerate(context):
        key, sep, value = line.partition("=")
        if sep and key.strip() == "filesystems":
            entries = [e for e in value.strip().split(";") if e]
            if share not in entries:
                entries.append(share)
            context[i] = "filesystems=" + ";".join(entries) + ";"
            break
    else:
        context.append(f"filesystems={share};")

    environment = group("Environment")
    for i, line in enumerate(environment):
        key, sep, _value = line.partition("=")
        if sep and key.strip() in wanted_env:
            environment[i] = f"{key.strip()}={wanted_env.pop(key.strip())}"
    environment.extend(f"{k}={v}" for k, v in wanted_env.items())

    out: list[str] = []
    for name, lines in groups:
        body = [l for l in lines if l.strip()]
        if name:
            if out:
                out.append("")
            out.append(f"[{name}]")
        out.extend(body)
    return "\n".join(out).lstrip("\n") + "\n"


def sync_flatpak() -> bool:
    """Bring the copy in line with the system bundle and make sure the
    override hands it to every Flatpak app. Returns whether either changed.
    Run on every apply that needs the CA, so a new CA, or new roots from an
    OS update, reach the copy too."""
    changed = False
    FLATPAK_CA_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(FLATPAK_CA_DIR, 0o755)
    changed |= _write_if_changed(FLATPAK_BUNDLE, SYSTEM_BUNDLE.read_bytes(), 0o644)
    FLATPAK_OVERRIDE.parent.mkdir(parents=True, exist_ok=True)
    try:
        existing = FLATPAK_OVERRIDE.read_text()
    except OSError:
        existing = ""
    changed |= _write_if_changed(FLATPAK_OVERRIDE, render_override(existing).encode(), 0o644)
    if changed:
        log.info("Flatpak apps given the system certificate bundle (%s)", FLATPAK_BUNDLE)
    return changed


def _sync_flatpak_quietly() -> None:
    try:
        sync_flatpak()
    except OSError as e:
        log.warning("could not give Flatpak apps the certificate bundle: %s", e)


def flatpak_problem() -> str | None:
    """What is wrong with the Flatpak copy, in words, or None."""
    try:
        if FLATPAK_BUNDLE.read_bytes() != SYSTEM_BUNDLE.read_bytes():
            return ("apps from the Store have an old copy of this computer's "
                    "certificates, so some of them cannot connect")
    except OSError:
        return ("apps from the Store cannot see this computer's certificates, "
                "so some of them cannot connect")
    return None
