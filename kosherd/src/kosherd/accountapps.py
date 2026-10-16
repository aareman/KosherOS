"""Apps that live in one account's own home rather than system-wide: the
Nix packages (nixapps) and the developers' own builds (upstreamapps).

Both are installed for the account that asks, as that account, and both
answer the same questions — is this ref one of mine, what does this
account have, install, upgrade, remove — so the daemon asks here and does
not care which of the two it is.
"""

from __future__ import annotations

from . import nixapps, upstreamapps

ERRORS = (nixapps.NixAppError, upstreamapps.UpstreamAppError)


def kind(ref: str):
    if nixapps.is_nix(ref):
        return nixapps
    if upstreamapps.is_upstream(ref):
        return upstreamapps
    return None


def is_account_app(ref: str) -> bool:
    return kind(ref) is not None


def package(ref: str) -> str:
    return kind(ref).package(ref)


def installed(uid: int) -> set[str]:
    return set(nixapps.installed(uid)) | upstreamapps.installed(uid)


def _act(name: str, ref: str, uid: int, on_progress, as_root: bool) -> None:
    """`as_root` is for Nix only (#123): an account without developer tools
    is refused by the Nix daemon, so its Store installs run as root into
    its profile. A developer's own build never touches the daemon and is
    always installed as the account."""
    module = kind(ref)
    if module is nixapps:
        getattr(nixapps, name)(ref, uid, on_progress, as_root=as_root)
    else:
        getattr(module, name)(ref, uid, on_progress)


def install(ref: str, uid: int, on_progress, as_root: bool = False) -> None:
    _act("install", ref, uid, on_progress, as_root)


def upgrade(ref: str, uid: int, on_progress, as_root: bool = False) -> None:
    _act("upgrade", ref, uid, on_progress, as_root)


def remove(ref: str, uid: int, on_progress, as_root: bool = False) -> None:
    _act("remove", ref, uid, on_progress, as_root)


def exists(ref: str) -> bool | None:
    return kind(ref).exists(ref)
