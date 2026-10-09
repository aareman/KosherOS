"""Reconstruct ChatGPT delta-v1 channels before inspecting their content.

The upstream and downstream documents are separate: edits to checked output
must never corrupt the paths inherited by the next upstream delta.
"""

import copy
from dataclasses import dataclass
from typing import Final, final

from .ai_json import mapping
from .ai_text import OutputBlocked

OPERATIONS: Final = frozenset({"add", "replace", "append", "remove", "truncate", "patch"})
MAX_PATH: Final = 2048
MAX_CHANNEL: Final = 1024


@dataclass(frozen=True, slots=True)
class Patch:
    p: str = ""
    o: str = "add"
    v: object = None
    c: int = 0

    @classmethod
    def parse(cls, data, previous: "Patch | None" = None) -> "Patch":
        """One wire patch; a field left out inherits from the previous one."""
        item = mapping(data)
        base = previous or cls()
        path = item.get("p", base.p)
        operation = item.get("o", base.o)
        channel = item.get("c", base.c)
        if not isinstance(path, str) or len(path) > MAX_PATH:
            raise OutputBlocked("Invalid AI patch path")
        if operation not in OPERATIONS:
            raise OutputBlocked("Unsupported AI patch operation")
        if not isinstance(channel, int) or isinstance(channel, bool) \
                or channel < 0 or channel > MAX_CHANNEL:
            raise OutputBlocked("Invalid AI patch channel")
        return cls(path, operation, item.get("v"), channel)

    def narrowed(self, path: str) -> "Patch":
        return Patch(path, self.o, self.v, self.c)


def apply(root, patch: Patch, depth: int = 0):
    """Apply bounded delta operations to a private copy of a channel document."""
    if depth > 32:
        raise OutputBlocked("AI patch nesting exceeds the inspection limit")
    if patch.p:
        if not patch.p.startswith("/"):
            raise OutputBlocked("Invalid AI patch path")
        token, _, rest = patch.p[1:].partition("/")
        token = token.replace("~1", "/").replace("~0", "~")
        child = patch.narrowed("/" + rest if rest else "")
        if isinstance(root, dict):
            result = root.copy()
            if not rest and patch.o == "remove":
                result.pop(token, None)
            else:
                result[token] = apply(result.get(token), child, depth + 1)
            return result
        if isinstance(root, list):
            if not token.isdecimal() or int(token) > len(root):
                raise OutputBlocked("Invalid AI array patch")
            index = int(token)
            items = root.copy()
            if not rest and patch.o == "add":
                items.insert(index, patch.v)
            elif index >= len(items):
                raise OutputBlocked("Invalid AI array index")
            elif not rest and patch.o == "remove":
                items.pop(index)
            else:
                items[index] = apply(items[index], child, depth + 1)
            return items
        raise OutputBlocked("AI patch traverses a scalar")
    if patch.o in {"add", "replace"}:
        return copy.deepcopy(patch.v)
    if patch.o == "remove":
        return None
    if patch.o == "append":
        if isinstance(root, str) and isinstance(patch.v, str):
            return root + patch.v
        if isinstance(root, list) and isinstance(patch.v, list):
            return root + patch.v
        if isinstance(root, dict) and isinstance(patch.v, dict):
            return root | patch.v
        raise OutputBlocked("Incompatible AI append operation")
    if patch.o == "truncate":
        if isinstance(patch.v, int) and not isinstance(patch.v, bool) \
                and patch.v >= 0 and isinstance(root, (str, list)):
            return root[:patch.v]
        raise OutputBlocked("Invalid AI truncation")
    # "patch": a batch of nested operations.
    if not isinstance(patch.v, list) or len(patch.v) > 512:
        raise OutputBlocked("Invalid AI patch batch")
    result = root
    for item in patch.v:
        result = apply(result, Patch.parse(item), depth + 1)
    return result


@final
class Channels:
    """Retain upstream headers and documents for one response or WS topic."""

    def __init__(self) -> None:
        self.previous = Patch()
        self.documents: dict[int, object] = {}

    def update(self, value) -> tuple[int, object]:
        patch = Patch.parse(value, self.previous)
        if patch.c not in self.documents and len(self.documents) >= 32:
            raise OutputBlocked("Too many AI output channels")
        document = apply(self.documents.get(patch.c), patch)
        self.documents[patch.c] = document
        self.previous = patch
        return patch.c, document
