"""URL rules for inspect mode — the only place path-level filtering exists.

At the DNS/IP layer a request is just "some host"; the path is encrypted.
Inspect mode terminates TLS locally (see mitm/), so rules here can talk
about full URLs.

Rule syntax, in the order an admin would write it:

    youtube.com/watch*        block a path under a host
    *.example.com             a host and all of its subdomains
    example.com/safe/*        anything below a directory
    *                         everything (a useful last rule)

Rules are an ordered list; the FIRST match decides, so specific rules go
above general ones. If nothing matches, the request is allowed — inspect
mode sits on top of the family-DNS baseline, so it filters rather than
whitelists unless the admin ends the list with a `block *`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

ALLOW = "allow"
BLOCK = "block"
ACTIONS = (ALLOW, BLOCK)


class RuleError(ValueError):
    pass


@dataclass(frozen=True)
class Rule:
    action: str
    pattern: str
    _regex: re.Pattern = None  # type: ignore[assignment]

    def __post_init__(self):
        if self.action not in ACTIONS:
            raise RuleError(f"unknown action {self.action!r}")
        if not self.pattern.strip():
            raise RuleError("pattern must not be empty")
        object.__setattr__(self, "_regex", _compile(self.pattern))

    def matches(self, host: str, path: str) -> bool:
        return bool(self._regex.match(_canonical(host, path)))

    def to_dict(self) -> dict:
        return {"action": self.action, "pattern": self.pattern}


def _canonical(host: str, path: str) -> str:
    """host + path, lowercased, without a leading 'www.' or trailing slash."""
    host = host.lower().strip(".")
    host = host.removeprefix("www.")
    if not path.startswith("/"):
        path = "/" + path
    if len(path) > 1:
        path = path.rstrip("/")
    return host + path


def _compile(pattern: str) -> re.Pattern:
    p = pattern.strip().lower()
    for prefix in ("https://", "http://"):
        p = p.removeprefix(prefix)
    p = p.removeprefix("www.")

    # A bare host (no path) covers the whole site.
    if "/" not in p:
        p += "/*"
    host, _, path = p.partition("/")
    path = "/" + path

    # "*.example.com" means the domain AND its subdomains.
    if host.startswith("*."):
        base = re.escape(host[2:])
        host_re = rf"(?:[^/]+\.)?{base}"
    else:
        host_re = _glob(host)

    path_re = _glob(path)
    # A trailing /* also matches the bare directory itself.
    if path.endswith("/*"):
        path_re = _glob(path[:-2]) + r"(?:/.*)?"
    # Case-insensitive: paths are technically case-sensitive, but a rule that
    # /videos blocks must not be dodged by asking for /Videos.
    return re.compile(rf"^{host_re}{path_re}$", re.IGNORECASE)


def _glob(text: str) -> str:
    return "".join(".*" if ch == "*" else re.escape(ch) for ch in text)


def parse_rules(raw: list[dict]) -> list[Rule]:
    return [Rule(action=r["action"], pattern=r["pattern"]) for r in raw]


def decide(rules: list[Rule], url: str) -> tuple[str, str | None]:
    """Return (action, matched pattern) for a URL. Default: allow."""
    parts = urlsplit(url if "//" in url else f"//{url}")
    host = parts.hostname or ""
    path = parts.path or "/"
    if parts.query:
        path = f"{path}?{parts.query}"
    for rule in rules:
        if rule.matches(host, path):
            return rule.action, rule.pattern
    return ALLOW, None
