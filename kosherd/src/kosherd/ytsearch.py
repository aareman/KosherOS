"""Finding a YouTube channel by name, for the approved-channel list.

A parent knows a channel as "TorahAnytime", not as UC7BFmSXP4mHMNSvWUaqg2uQ,
and asking them to dig the ID out of a video's address is where approving
channels used to stop. This asks YouTube the same question its own search
page asks, limited to channels, and hands back what a person can recognise
next to what the filter matches on.

kosherd asks a short-lived sandbox to do the network work. Its dedicated
user can reach only the proxy's channel-search listener, which admits
this one YouTube endpoint. Neither fetching nor parsing YouTube's answer
runs as root; the administrator's own YouTube limits do not affect it.

YouTube's web client needs no API key for this. Its answer is an internal
format, so the parsing is by shape and forgiving: a renderer it does not
recognise is skipped, never an error.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

__all__ = ["SearchError", "parse_channels", "search_channels"]

SEARCH_URL = "https://www.youtube.com/youtubei/v1/search?prettyPrint=false"
# The web client as YouTube's own results page names itself. The version
# only has to be one YouTube still accepts; it is not a promise of layout.
CLIENT = {"clientName": "WEB", "clientVersion": "2.20250101.00.00",
          "hl": "en", "gl": "US"}
# The "Type: Channel" filter on YouTube's search page.
CHANNELS_ONLY = "EgIQAg=="
MAX_DEPTH = 32
SERVICE_USER = "kosher-ytsearch"
MAX_QUERY = 256
MAX_RESPONSE = 2 * 1024 * 1024
MAX_RESULT = 64 * 1024
RUNTIME_DIR = Path("/run/kosherd")

# Kept together so the live acceptance check can start the same sandbox
# with a harmless probe. Nix's own sandbox is different: unlike this
# helper it needs mount namespaces, ownership changes and build users.
SANDBOX = (
    "ProtectSystem=strict", "ProtectHome=yes", "PrivateTmp=yes",
    "PrivateDevices=yes", "NoNewPrivileges=yes", "CapabilityBoundingSet=",
    "ProtectKernelTunables=yes", "ProtectKernelModules=yes",
    "ProtectKernelLogs=yes", "ProtectControlGroups=yes", "ProtectClock=yes",
    "RestrictNamespaces=yes", "RestrictSUIDSGID=yes", "LockPersonality=yes",
    "MemoryDenyWriteExecute=yes", "SystemCallArchitectures=native",
    "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6", "UMask=0077",
    "MemoryMax=128M", "TasksMax=16", "LimitFSIZE=65536", "RuntimeMaxSec=20",
)


class SearchError(Exception):
    """YouTube could not be asked, or its answer could not be read."""


def search_channels(query: str) -> list[dict]:
    """Ask the unprivileged sandbox. No network parsing in the daemon."""
    query = _query(query)
    if not query:
        return []
    argv = ["systemd-run", "--quiet", "--pipe", "--wait", "--collect",
            "--service-type=exec", f"--uid={SERVICE_USER}", f"--gid={SERVICE_USER}",
            "--property=Requires=kosher-mitm.service",
            "--property=After=kosher-firewall.service kosher-mitm.service",
            *(f"--property={p}" for p in SANDBOX),
            "/usr/bin/python3", "-I", "-m", "kosherd.ytsearch", "--worker"]
    # Regular files, not an unbounded communicate() pipe: LimitFSIZE also
    # bounds a compromised helper's output before the root daemon reads it.
    # /tmp's user_tmp_t label cannot be handed to PID1 on Fedora SELinux.
    # kosherd's existing root-only runtime directory has the service label.
    with (tempfile.TemporaryFile(dir=RUNTIME_DIR) as output,
          tempfile.TemporaryFile(dir=RUNTIME_DIR) as errors):
        try:
            result = subprocess.run(argv, input=json.dumps({"query": query}),
                                    text=True, stdout=output, stderr=errors, timeout=25)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise SearchError("the channel search service could not finish") from e
        output.seek(0)
        data = output.read(MAX_RESULT + 1)
    if len(data) > MAX_RESULT:
        raise SearchError("the channel search service returned too much data")
    if not data and result.returncode:
        raise SearchError("the channel search could not start")
    try:
        doc = json.loads(data)
        if isinstance(doc, dict) and isinstance(doc.get("error"), str):
            raise SearchError(doc["error"][:512])
        channels = doc["channels"]
        if result.returncode != 0 or not isinstance(channels, list) or len(channels) > 20:
            raise ValueError("bad result")
        for channel in channels:
            if (not isinstance(channel, dict) or not isinstance(channel.get("id"), str)
                    or not channel["id"].startswith("UC")
                    or not isinstance(channel.get("title"), str)
                    or any(k not in {"id", "title", "handle", "subscribers"}
                           or not isinstance(v, str) or len(v) > 1024
                           for k, v in channel.items())):
                raise ValueError("bad channel")
        return channels
    except (ValueError, TypeError, KeyError) as e:
        raise SearchError("the channel search service sent an unreadable answer") from e


def _query(query: str) -> str:
    if not isinstance(query, str) or len(query) > MAX_QUERY:
        raise SearchError(f"a channel search must be at most {MAX_QUERY} characters")
    return query.strip()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _open(request, *, timeout):
    # The fixed HTTPS endpoint must not send the helper to another site,
    # and environment proxy variables cannot supply a second route out.
    return urllib.request.build_opener(_NoRedirect(), urllib.request.ProxyHandler({})).open(
        request, timeout=timeout)


def _fetch_channels(query: str, limit: int = 20, timeout: float = 15.0) -> list[dict]:
    """Channels matching `query`, best first: each has `id` (UC…),
    `title`, and when YouTube says so `handle` (@…) and `subscribers`."""
    query = _query(query)
    if not query:
        return []
    body = json.dumps({"context": {"client": CLIENT}, "query": query,
                       "params": CHANNELS_ONLY}).encode()
    request = urllib.request.Request(
        SEARCH_URL, data=body,
        headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"})
    try:
        with _open(request, timeout=timeout) as response:
            data = response.read(MAX_RESPONSE + 1)
            if len(data) > MAX_RESPONSE:
                raise SearchError("YouTube's search sent too much data")
            doc = json.loads(data)
    except urllib.error.HTTPError as e:
        raise SearchError(f"YouTube's search answered HTTP {e.code}") from e
    except (urllib.error.URLError, OSError) as e:
        reason = getattr(e, "reason", e)
        raise SearchError(f"cannot reach YouTube: {reason}") from e
    except ValueError as e:
        raise SearchError(f"YouTube's search sent something unreadable: {e}") from e
    return parse_channels(doc)[:limit]


def worker() -> int:
    """The only network entry point. Refuse accidental execution as root."""
    try:
        if os.getuid() == 0:
            raise SearchError("the channel search helper must not run as root")
        raw = sys.stdin.buffer.read(4097)
        if len(raw) > 4096:
            raise SearchError("the channel search request is too large")
        doc = json.loads(raw)
        channels = _fetch_channels(_query(doc["query"]))
        result = json.dumps({"channels": channels})
        if len(result.encode()) > MAX_RESULT:
            raise SearchError("YouTube's search sent too much channel data")
        print(result)
        return 0
    except (SearchError, ValueError, TypeError, KeyError, RecursionError) as e:
        print(json.dumps({"error": str(e)[:512]}))
        return 1


def parse_channels(doc) -> list[dict]:
    """Every channel in a search answer, in the order YouTube ranked them,
    each channel once."""
    found: list[dict] = []
    seen: set[str] = set()
    for renderer in _renderers(doc, 0):
        channel = _channel(renderer)
        if channel and channel["id"] not in seen:
            seen.add(channel["id"])
            found.append(channel)
    return found


def _renderers(node, depth: int):
    if depth > MAX_DEPTH:
        return
    if isinstance(node, dict):
        renderer = node.get("channelRenderer")
        if isinstance(renderer, dict):
            yield renderer
        for value in node.values():
            yield from _renderers(value, depth + 1)
    elif isinstance(node, list):
        for value in node:
            yield from _renderers(value, depth + 1)


def _channel(renderer: dict) -> dict | None:
    channel_id = renderer.get("channelId")
    if not isinstance(channel_id, str) or not channel_id.startswith("UC"):
        return None
    channel = {"id": channel_id, "title": _text(renderer.get("title")) or channel_id}
    endpoint = (renderer.get("navigationEndpoint") or {}).get("browseEndpoint") or {}
    base = endpoint.get("canonicalBaseUrl") or ""
    if base.startswith("/@"):
        channel["handle"] = base[1:]
    # Since handles arrived, YouTube shows the handle in the field named
    # for the subscriber count and the count in the one named for videos.
    # Take whichever of the two reads as a count.
    for key in ("videoCountText", "subscriberCountText"):
        text = _text(renderer.get(key))
        if text and "subscriber" in text:
            channel["subscribers"] = text
            break
    return channel


def _text(node) -> str:
    if not isinstance(node, dict):
        return ""
    if isinstance(node.get("simpleText"), str):
        return node["simpleText"]
    return "".join(run.get("text", "") for run in node.get("runs") or []
                   if isinstance(run, dict))


if __name__ == "__main__":
    raise SystemExit(worker() if sys.argv[1:] == ["--worker"] else 2)
