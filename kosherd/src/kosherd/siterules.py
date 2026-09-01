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

# The query parameter a shop's search box uses, when its rules do not say.
DEFAULT_SEARCH_PARAMS = ("q", "k", "query", "search", "keyword", "term")
# What an autocomplete endpoint's path looks like, when its rules do not say.
DEFAULT_SUGGEST_PATHS = ("suggest", "autosug", "autocomplete", "typeahead",
                         "complete", "/ac/", "instant")

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
        # Terms that block a SEARCH on a shop, pooled across every site:
        # somebody typing "lingerie" into a shop's own box means the same
        # thing whichever shop it is, and the department lists are already
        # exactly that vocabulary.
        pooled = {t for site in self.sites for t in site.get("terms", [])}
        pooled |= set(anywhere or [])
        self._search_terms = _compile(sorted(pooled))

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


    def _site_for(self, host: str) -> dict | None:
        for hosts, _pattern, site in self._by_host:
            if any(_under(host, h) for h in hosts):
                return site
        return None

    def covers(self, host: str) -> bool:
        """Does this host have rules of its own?"""
        return self._site_for((host or "").lower().strip(".")) is not None

    def search_text(self, url: str) -> str | None:
        """What was typed into this site's own search box, if anything.

        A department rule is worth nothing if the box on the same page
        reaches the department anyway.
        """
        parts = urlsplit(url if "//" in url else "//" + url)
        site = self._site_for((parts.hostname or "").lower().strip("."))
        if site is None:
            return None
        wanted = site.get("search_params") or DEFAULT_SEARCH_PARAMS
        for key, value in parse_qsl(parts.query):
            if key in wanted and value.strip():
                return unquote_plus(value)
        return None

    def blocked_term(self, text: str) -> str | None:
        """The department term in this text, if any.

        Used on a typed search and on each autocomplete entry. A single
        word like "lingerie" is a department name, not a page: the content
        scorer needs a page's worth of evidence and will never see it here,
        so the term list is what has to answer.
        """
        if not text or self._search_terms is None:
            return None
        match = self._search_terms.search(text.lower())
        return match.group(0) if match else None

    def blocked_search(self, url: str) -> str | None:
        """Why a search on this site must not run, or None."""
        term = self.blocked_term(self.search_text(url) or "")
        return f"a search for {term}" if term else None

    def is_suggestions(self, url: str) -> bool:
        """Is this a shop's autocomplete endpoint?

        Worth its own answer because autocomplete is worse than the
        results: it puts the words on screen unprompted, while somebody is
        typing something else.
        """
        parts = urlsplit(url if "//" in url else "//" + url)
        host = (parts.hostname or "").lower().strip(".")
        path = (parts.path or "").lower()
        for hosts, _pattern, site in self._by_host:
            extra = tuple(h.lower().strip(".")
                          for h in site.get("suggest_hosts", []))
            if not (any(_under(host, h) for h in hosts)
                    or any(_under(host, h) for h in extra)):
                continue
            markers = site.get("suggest_paths") or DEFAULT_SUGGEST_PATHS
            if any(marker in path for marker in markers):
                return True
        return False


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
