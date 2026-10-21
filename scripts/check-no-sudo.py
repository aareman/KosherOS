#!/usr/bin/env python3
"""Fail if a just recipe, or anything it depends on, would run sudo.

    scripts/check-no-sudo.py [RECIPE ...]

With no recipe named, checks the ones `just pr-test` runs. A recipe counts
as needing sudo when its body runs `sudo` or it depends on `_sudo`, and so
does every recipe that depends on one of those. The scripts the harness
runs itself are read for a sudo call too.

This is the static half of the rule that the VM harness never needs root.
The other half is at run time: scripts/pr-test.py puts a `sudo` first on
the PATH that fails and records the attempt.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
HARNESS_RECIPES = ["build", "vm-rootless", "test-boot", "pr-test"]
HARNESS_SCRIPTS = ["scripts/pr-test.py", "scripts/rootless-disk.sh", "scripts/boot-test.py"]

# A sudo that runs: at the start of a line or after a shell separator, and
# not inside a comment or a quoted message about sudo.
SUDO = re.compile(r"(^|[;&|(`]|\$\()\s*sudo\b")


def recipes() -> dict:
    out = subprocess.run(["just", "--dump", "--dump-format", "json"], cwd=REPO,
                         capture_output=True, text=True, check=True).stdout
    return json.loads(out)["recipes"]


def body_text(recipe: dict) -> list[str]:
    lines = []
    for fragments in recipe.get("body", []):
        lines.append("".join(f if isinstance(f, str) else "{{...}}" for f in fragments))
    return lines


def runs_sudo(lines: list[str]) -> list[str]:
    hits = []
    for line in lines:
        code = line.strip()
        if code.startswith("#"):
            continue
        code = re.sub(r"(\"[^\"]*\"|'[^']*')", '""', code)
        if SUDO.search(code):
            hits.append(line.strip())
    return hits


def problems(names: list[str], table: dict) -> list[str]:
    found, seen = [], set()

    def walk(name: str, path: list[str]) -> None:
        if name in seen:
            return
        seen.add(name)
        recipe = table.get(name)
        if recipe is None:
            found.append(f"{' -> '.join(path)}: no such recipe")
            return
        if name == "_sudo":
            found.append(f"{' -> '.join(path)}: depends on _sudo")
            return
        for hit in runs_sudo(body_text(recipe)):
            found.append(f"{' -> '.join(path)}: runs `{hit}`")
        for dep in recipe.get("dependencies", []):
            walk(dep["recipe"], path + [dep["recipe"]])

    for name in names:
        walk(name, [name])
    return found


# In Python, sudo is run as a list element or at the start of a command string.
PY_SUDO = re.compile(r"""["']sudo["']\s*[,\]]|["']sudo\s""")


def script_problems() -> list[str]:
    found = []
    for rel in HARNESS_SCRIPTS:
        path = REPO / rel
        if not path.exists():
            continue
        lines = [ln for ln in path.read_text().splitlines() if not ln.lstrip().startswith("#")]
        if rel.endswith(".py"):
            hits = [ln.strip() for ln in lines if PY_SUDO.search(ln)]
        else:
            hits = runs_sudo(lines)
        found += [f"{rel}: runs `{hit}`" for hit in hits]
    return found


def main() -> int:
    names = sys.argv[1:] or HARNESS_RECIPES
    found = problems(names, recipes()) + script_problems()
    if found:
        print("These would need sudo, and the VM harness must never use it:")
        for line in found:
            print(f"  {line}")
        return 1
    print(f"no sudo in: {', '.join(names)} (and the harness scripts)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
