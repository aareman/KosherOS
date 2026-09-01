"""Filtering a site's own search box and its autocomplete.

Blocking the lingerie department gets you nothing if the search box on the
same page reaches it anyway, and autocomplete is worse than the results:
it puts the words on screen unprompted, while somebody is typing something
else. A child typing "b" into Amazon should not be offered five ways to
finish it.

Suggestions come back as JSON over XHR, and every site's shape is
different — Amazon nests them under `suggestions`, eBay under `res.sug`,
others return a bare array. Rather than learn each schema and re-learn it
whenever one changes, this walks whatever came back and drops LIST
elements whose text is blocked, leaving the structure alone. Dropping
entries from a list of suggestions is exactly the right edit, and it is
the one edit that cannot break a response's shape.
"""

from __future__ import annotations

import json
import logging

log = logging.getLogger(__name__)

# A suggestion response is small. Anything this large is not one, and
# walking it would cost more than it is worth.
MAX_BYTES = 512 * 1024
MAX_DEPTH = 12


def text_of(node, depth: int = 0) -> str:
    """Every string in a node, flattened — what a reader would see."""
    if depth > MAX_DEPTH:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return " ".join(text_of(item, depth + 1) for item in node)
    if isinstance(node, dict):
        return " ".join(text_of(value, depth + 1) for value in node.values())
    return ""


def prune(node, is_blocked, depth: int = 0):
    """Drop blocked entries from every list in `node`, structure intact."""
    if depth > MAX_DEPTH:
        return node
    if isinstance(node, list):
        kept = []
        for item in node:
            text = text_of(item)
            if text and is_blocked(text):
                continue
            kept.append(prune(item, is_blocked, depth + 1))
        return kept
    if isinstance(node, dict):
        return {key: prune(value, is_blocked, depth + 1)
                for key, value in node.items()}
    return node


def filter_json(body: bytes, is_blocked) -> bytes | None:
    """Filter a suggestion response. None means it was left alone."""
    if not body or len(body) > MAX_BYTES:
        return None
    try:
        document = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return None
    pruned = prune(document, is_blocked)
    if pruned == document:
        return None
    return json.dumps(pruned, separators=(",", ":")).encode()
