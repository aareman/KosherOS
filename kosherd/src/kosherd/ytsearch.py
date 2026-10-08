"""Finding a YouTube channel by name, for the approved-channel list.

A parent knows a channel as "TorahAnytime", not as UC7BFmSXP4mHMNSvWUaqg2uQ,
and asking them to dig the ID out of a video's address is where approving
channels used to stop. This asks YouTube the same question its own search
page asks, limited to channels, and hands back what a person can recognise
next to what the filter matches on.

It runs inside kosherd rather than the admin app: the daemon's traffic is
not filtered, while the administrator's own account may be — and a search
for channels to allow should not itself be refused by the YouTube limits.

YouTube's web client needs no API key for this. Its answer is an internal
format, so the parsing is by shape and forgiving: a renderer it does not
recognise is skipped, never an error.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

__all__ = ["SearchError", "parse_channels", "search_channels"]

SEARCH_URL = "https://www.youtube.com/youtubei/v1/search?prettyPrint=false"
# The web client as YouTube's own results page names itself. The version
# only has to be one YouTube still accepts; it is not a promise of layout.
CLIENT = {"clientName": "WEB", "clientVersion": "2.20250101.00.00",
          "hl": "en", "gl": "US"}
# The "Type: Channel" filter on YouTube's search page.
CHANNELS_ONLY = "EgIQAg=="
MAX_DEPTH = 32


class SearchError(Exception):
    """YouTube could not be asked, or its answer could not be read."""


def search_channels(query: str, limit: int = 20, timeout: float = 15.0) -> list[dict]:
    """Channels matching `query`, best first: each has `id` (UC…),
    `title`, and when YouTube says so `handle` (@…) and `subscribers`."""
    query = (query or "").strip()
    if not query:
        return []
    body = json.dumps({"context": {"client": CLIENT}, "query": query,
                       "params": CHANNELS_ONLY}).encode()
    request = urllib.request.Request(
        SEARCH_URL, data=body,
        headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            doc = json.loads(response.read())
    except urllib.error.HTTPError as e:
        raise SearchError(f"YouTube's search answered HTTP {e.code}") from e
    except (urllib.error.URLError, OSError) as e:
        reason = getattr(e, "reason", e)
        raise SearchError(f"cannot reach YouTube: {reason}") from e
    except ValueError as e:
        raise SearchError(f"YouTube's search sent something unreadable: {e}") from e
    return parse_channels(doc)[:limit]


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
