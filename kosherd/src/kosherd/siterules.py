"""Reading what a big site already says about itself.

Blocking women's clothing on Amazon is not a vision problem. The
department is in the address: a search carries a department parameter, a
listing page carries a node id, a product page carries breadcrumbs. Running
a model over forty product thumbnails to discover what the URL states in
plain text would be slower, less accurate, and pointless — and it would
mistake a sofa for a person often enough to matter.

So the large shopping sites get read rather than looked at. It costs
microseconds, it happens on the REQUEST, before a byte of the page is
fetched, and it is auditable: an admin can see exactly which word in which
address caused a block, and disagree with it.

This is deliberately not a domain category. Amazon is not a site a family
blocks; a department on it is. Rules here therefore describe a *part* of
a site, and only apply to accounts that block the `immodest` category —
the same setting that blocks lingerie retailers outright.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import parse_qsl, unquote_plus, urlsplit

RULES_PATHS = (
    Path("/var/lib/kosher/site-rules.json"),    # admin- or portal-supplied
    Path("/usr/share/kosher/site-rules.json"),  # shipped default
)

# The category that turns these on. A family blocking lingerie retailers
# means the lingerie department too.
GATING_CATEGORY = "immodest"

# What a category listing's address looks like on a shop nobody has
# written rules for.
SHOP_SHAPED = re.compile(
    r"(?<![a-z0-9])(shop|shops|store|collections?|categor(?:y|ies)|dept|"
    r"department|browse|products?|catalog|c|b|s)(?![a-z0-9])")


class SiteRules:
    """Per-site department rules, matched against a URL."""

    def __init__(self, sites: list[dict] | None = None,
                 anywhere: list[str] | None = None):
        self.sites = sites or []
        self._anywhere = _compile(anywhere or [])
        self._by_host: list[tuple[tuple[str, ...], re.Pattern | None, dict]] = []
        for site in self.sites:
            hosts = tuple(h.lower().strip(".") for h in site.get("hosts", []))
            self._by_host.append((hosts, _compile(site.get("terms", [])), site))

    def __len__(self) -> int:
        return len(self.sites)

    def reason(self, url: str) -> str | None:
        """Why this address is a blocked department, or None."""
        parts = urlsplit(url if "//" in url else "//" + url)
        host = (parts.hostname or "").lower().strip(".")
        if not host:
            return None
        # Decoded, because a department reaches the server percent-encoded
        # as often as not.
        haystack = unquote_plus(f"{parts.path}?{parts.query}").lower()

        for hosts, pattern, site in self._by_host:
            if not any(_under(host, h) for h in hosts):
                continue
            for key, values in (site.get("query") or {}).items():
                found = {v.lower() for k, v in parse_qsl(parts.query) if k == key}
                hit = found & {v.lower() for v in values}
                if hit:
                    return f"the {sorted(hit)[0]} department of {host}"
            if pattern is not None:
                match = pattern.search(haystack)
                if match:
                    return f"the {match.group(0)} section of {host}"
            # A site with rules is fully described by them; do not also
            # apply the generic list, which is tuned for unknown shops.
            return None

        # For a shop nobody has written rules for, the term alone is not
        # enough: an article whose title contains the word is not a
        # department. Require the address to look like a category listing,
        # and leave everything else to the page scorer.
        if self._anywhere is not None and SHOP_SHAPED.search(haystack):
            match = self._anywhere.search(haystack)
            if match:
                return f"a {match.group(0)} section"
        return None


def _under(host: str, domain: str) -> bool:
    return host == domain or host.endswith("." + domain)


def _compile(terms: list[str]) -> re.Pattern | None:
    if not terms:
        return None
    # Bounded by non-word characters rather than \b: these appear in URLs
    # as /womens-lingerie/ and ?dept=lingerie, not as free text.
    alts = "|".join(re.escape(t.lower()).replace(r"\ ", r"[\s\-_+]") for t in
                    sorted(terms, key=len, reverse=True))
    return re.compile(rf"(?<![a-z0-9])({alts})(?![a-z0-9])")


def load(*paths: Path) -> SiteRules:
    for path in (paths or RULES_PATHS):
        try:
            doc = json.loads(Path(path).read_text())
        except (OSError, ValueError):
            continue
        return SiteRules(doc.get("sites", []), doc.get("anywhere", []))
    return SiteRules()
