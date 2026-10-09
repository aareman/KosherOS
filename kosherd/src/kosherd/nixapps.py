"""Apps from nixpkgs: the second kind of app the Store carries.

Most AI coding tools and a few editors (issue #34) have no Flatpak, and
KosherOS ships no package repository of its own. Nix is on every machine,
and nixpkgs carries them all — so a catalog entry whose ref is
`nixpkgs#<package>` is installed from upstream nixpkgs the way a Flatpak
entry is installed from upstream Flathub: only by kosherd, only for an
account appaccess says may have it.

Where a Flatpak is installed system-wide, a Nix app goes into the asking
account's OWN profile (~/.local/state/nix/profiles/profile), run as that
account. That is what makes the approval per account without a second
run-time gate: nobody else's PATH sees it, and the Nix daemon itself
refuses an account whose kind of internet is "No internet"
(nixdaemon.py). The account's side of the install — evaluating nixpkgs,
fetching its tarball — goes through that account's filter like anything
else it opens; the build's fetches run as the build users, which the
firewall holds to the registries.

Unfree packages (Claude Code, Cursor, Copilot) are allowed: the licence
is the publisher's to grant and the person installing is the one who
agrees to it, which is also what Flathub's non-free apps come to.
"""

from __future__ import annotations

import json
import logging
import pwd
import shutil
import subprocess
from collections import deque
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)

PREFIX = "nixpkgs#"
# The bundle the account's nix client trusts (kosher-ca.sh sets the same).
CA_BUNDLE = "/etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem"


class NixAppError(Exception):
    pass


def is_nix(ref: str) -> bool:
    return isinstance(ref, str) and ref.startswith(PREFIX)


def package(ref: str) -> str:
    """'nixpkgs#claude-code' -> 'claude-code'. Refuses anything that is not
    a plain attribute name: a ref is also an argument to nix."""
    name = ref[len(PREFIX):] if is_nix(ref) else ""
    if (not name or not name[0].isalnum()
            or not all(c.isalnum() or c in "-_." for c in name)):
        raise NixAppError(f"{ref!r} is not a nixpkgs package reference")
    return name


def profile_dir(uid: int) -> Path:
    """Where nix keeps this account's profile (the current generation)."""
    return Path(pwd.getpwuid(uid).pw_dir) / ".local/state/nix/profiles/profile"


def installed(uid: int) -> set[str]:
    """The refs in this account's profile, read from nix's own manifest.

    No nix invocation: the manifest is a small JSON file, and this is
    asked on every Store open. Version 3 keys the elements by package
    name; older manifests list them, named by their attribute path.
    """
    try:
        doc = json.loads((profile_dir(uid) / "manifest.json").read_text())
    except (KeyError, OSError, ValueError):
        return set()
    elements = doc.get("elements")
    found: set[str] = set()
    if isinstance(elements, dict):
        items = elements.items()
    elif isinstance(elements, list):
        items = ((e.get("attrPath", "").rsplit(".", 1)[-1], e) for e in elements
                 if isinstance(e, dict))
    else:
        return found
    for name, element in items:
        if name and isinstance(element, dict) and element.get("active", True):
            found.add(PREFIX + name)
    return found


def _argv(uid: int, *nix_args: str) -> list[str]:
    """`nix ...` as the account, with a clean environment of its own.

    runuser alone would hand nix root's HOME, and the profile would land
    in /root. --impure is what lets NIXPKGS_ALLOW_UNFREE through.
    """
    pw = pwd.getpwuid(uid)
    return [
        "runuser", "-u", pw.pw_name, "--",
        "env", "-i",
        f"HOME={pw.pw_dir}", f"USER={pw.pw_name}", f"LOGNAME={pw.pw_name}",
        "PATH=/usr/bin:/bin",
        "NIXPKGS_ALLOW_UNFREE=1",
        f"NIX_SSL_CERT_FILE={CA_BUNDLE}",
        "nix", *nix_args,
    ]


def _status(line: str) -> str:
    """What nix's log line means, in the words the Store's progress shows."""
    text = line.lower()
    if text.startswith(("copying path", "downloading", "unpacking", "fetching")):
        return "Downloading…"
    if text.startswith("building"):
        return "Building…"
    if "evaluating" in text or text.startswith("warning:"):
        return "Looking up…"
    return "Working…"


def _run(ref: str, argv: list[str], on_progress: Callable[[str, int, str], None]) -> None:
    proc = subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                            text=True)
    tail: deque[str] = deque(maxlen=20)
    assert proc.stderr is not None
    for raw in proc.stderr:
        line = raw.strip()
        if not line:
            continue
        tail.append(line)
        on_progress(ref, 0, _status(line))
    code = proc.wait()
    if code != 0:
        # The last line nix printed that is not a progress line is the
        # reason: "not allowed to connect", a hash mismatch, no network.
        reason = next((l for l in reversed(tail) if l.startswith("error")), None) \
            or (tail[-1] if tail else f"nix exited {code}")
        raise NixAppError(reason.removeprefix("error:").strip())


def _desktop(uid: int) -> None:
    """Link the profile's desktop files and icons into the account's own
    ~/.local/share (nixdesktop.py), as the account. Never a failure: an
    app that installed but has no desktop entry yet is still installed."""
    argv = _argv(uid)
    argv[argv.index("nix"):] = ["kosher-nix-desktop", "-q"]
    res = subprocess.run(argv, capture_output=True, text=True)
    if res.returncode != 0:
        log.warning("desktop entries for uid %d not linked: %s", uid, (res.stderr or "").strip())


def install(ref: str, uid: int, on_progress) -> None:
    name = package(ref)
    on_progress(ref, 0, "Looking up…")
    _run(ref, _argv(uid, "profile", "install", "--impure", PREFIX + name), on_progress)
    _desktop(uid)


def remove(ref: str, uid: int, on_progress) -> None:
    name = package(ref)
    on_progress(ref, 0, "Removing…")
    _run(ref, _argv(uid, "profile", "remove", name), on_progress)
    _desktop(uid)


def upgrade(ref: str, uid: int, on_progress) -> None:
    name = package(ref)
    on_progress(ref, 0, "Updating…")
    _run(ref, _argv(uid, "profile", "upgrade", "--impure", name), on_progress)
    _desktop(uid)


def exists(ref: str) -> bool | None:
    """Whether nixpkgs has this package: for `kosherctl check-catalog`.
    None where nix is not on this machine (nothing can be said)."""
    if shutil.which("nix") is None:
        return None
    name = package(ref)
    res = subprocess.run(
        ["env", "NIXPKGS_ALLOW_UNFREE=1", "nix", "eval", "--impure", "--raw",
         f"{PREFIX}{name}.name"],
        capture_output=True, text=True, timeout=600)
    return res.returncode == 0
