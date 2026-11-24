"""Which system services have failed, and what they said (#337).

There is no sudo on a KosherOS machine and an administrator's account cannot
read the system journal, so "bootloader-update.service failed" on a family
laptop could be seen but never explained. kosherd runs as root and reads it
for an administrator: the failed system units, each with how and when it
stopped and the last lines it logged. Read-only.
"""
from __future__ import annotations

import subprocess

# Enough to show an error and what led to it, not a whole log.
MAX_UNITS = 20
LOG_LINES = 30
MAX_LINE = 500

_PROPERTIES = ("Description", "Result", "ExecMainStatus", "ExecMainExitTimestamp",
               "InactiveEnterTimestamp", "NRestarts")


def _run(argv) -> str:
    res = subprocess.run(argv, capture_output=True, text=True, timeout=30)
    return res.stdout if res.returncode == 0 else ""


def failed_units(run=_run) -> list[str]:
    """The names of failed system units, as systemctl lists them."""
    out = run(["systemctl", "list-units", "--state=failed", "--plain",
               "--no-legend", "--no-pager"])
    names = []
    for line in out.splitlines():
        parts = line.split()
        if parts and "." in parts[0]:
            names.append(parts[0])
    return names[:MAX_UNITS]


def describe(unit: str, run=_run) -> dict:
    """One failed unit: systemd's account of it and its last log lines."""
    shown = run(["systemctl", "show", unit, "--no-pager",
                 *(f"--property={p}" for p in _PROPERTIES)])
    info = {}
    for line in shown.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            info[key] = value
    # This boot first; a unit that failed in an earlier boot and has not run
    # since has nothing there, so fall back to its last lines overall.
    log = []
    for scope in (["-b"], []):
        out = run(["journalctl", "--no-pager", "-o", "short-iso", "-n", str(LOG_LINES),
                   "-u", unit, *scope])
        log = [line[:MAX_LINE] for line in out.splitlines()
               if line.strip() and not line.startswith("-- ")]
        if log:
            break
    return {
        "unit": unit,
        "description": info.get("Description", ""),
        "result": info.get("Result", ""),
        "status": info.get("ExecMainStatus", ""),
        "failed_at": info.get("InactiveEnterTimestamp") or info.get("ExecMainExitTimestamp", ""),
        "restarts": info.get("NRestarts", ""),
        "log": log,
    }


def report(run=_run) -> dict:
    """{"failed": [describe(unit) for each failed system unit]}."""
    return {"failed": [describe(unit, run) for unit in failed_units(run)]}
