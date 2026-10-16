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


def install(ref: str, uid: int, on_progress) -> None:
    kind(ref).install(ref, uid, on_progress)


def upgrade(ref: str, uid: int, on_progress) -> None:
    kind(ref).upgrade(ref, uid, on_progress)


def remove(ref: str, uid: int, on_progress) -> None:
    kind(ref).remove(ref, uid, on_progress)


def exists(ref: str) -> bool | None:
    return kind(ref).exists(ref)
