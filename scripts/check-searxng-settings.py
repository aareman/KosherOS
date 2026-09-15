#!/usr/bin/env python3
"""Validate KosherOS's SearXNG settings against the SearXNG in the image.

Run at image build. Both halves of this caught a real bug that had already
shipped: an invalid `preferences.lock` entry stopped the engine starting at
all, and disabling an engine SearXNG does not have does not disable
anything — it defines a broken one. Lives in a file rather than a RUN
heredoc because the podman on GitHub's runners (4.9) does not understand
heredocs and read the Python inside as Containerfile instructions.
"""

import sys
import yaml

sys.path.insert(0, "/usr/share/kosher/searxng-src")
import searx  # noqa: F401 - raises on an invalid settings.yml
from searx import settings_loader

defaults, _ = settings_loader.load_settings(load_user_settings=False)
known = {e["name"] for e in defaults["engines"]}
ours = yaml.safe_load(
    open("/usr/share/kosher/searxng/settings.yml")).get("engines") or []
missing = sorted(e["name"] for e in ours if e["name"] not in known)
if missing:
    raise SystemExit(f"settings.yml names engines SearXNG does not ship: {missing}")
print(f"searxng settings valid; {len(ours)} engines disabled")
