"""Independent app-update scheduling and private, per-account notices."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path

from . import updates

TIMER = "kosher-auto-app-update.timer"
STATE_PATH = "/var/lib/kosher/auto-app-update.json"
MODES = ("update", "notify")


def load_state() -> dict:
    return updates.load_state(STATE_PATH)


def save_state(state: dict) -> None:
    # This file includes per-account app inventories. Only the daemon
    # reads it; ordinary accounts receive their own hashed notice tokens.
    path = Path(STATE_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    name = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=".app-updates-", delete=False) as f:
            name = f.name  # NamedTemporaryFile creates this with mode 0600.
            json.dump(state, f)
            f.write("\n")
        os.replace(name, path)
    finally:
        if name is not None:
            Path(name).unlink(missing_ok=True)


def notice(state: dict, uid: int) -> dict:
    """Only the shared Flatpaks and this account's Store-managed Nix apps.

    Tokens identify a particular offered build without exposing app names
    or another account's profile to the notification helper.
    """
    found = list(state.get("flatpak", [])) + list(state.get("accounts", {}).get(str(uid), []))
    tokens = sorted({hashlib.sha256(json.dumps(
        [a.get("ref"), a.get("latest"), a.get("commit")],
        separators=(",", ":")).encode()).hexdigest() for a in found})
    return {"updates": tokens, "count": len(tokens)}
