"""Result filtering for the built-in search engine.

A filter that only blocks pages makes searching miserable: every search is
a page of links, most of which lead to a block page, and the person has to
guess which ones will work. In whitelist mode it is worse than miserable —
the whitelist is invisible, so there is no way to find out what the
computer will let you read.

So KosherOS runs its own metasearch engine (SearXNG) on loopback and
filters the RESULTS with the same policy that filters the traffic. A
whitelist user searching for "kosher recipes" gets back only whitelisted
sites: the whitelist becomes browsable. A filtered user never sees a link
into a blocked category at all.

This module is the decision engine, deliberately kept out of the search
engine itself: SearXNG's plugin API changes between releases, so the
plugin (search/kosher_results.py) stays a thin shim over the functions
here, which are testable without SearXNG installed.

Which user is searching is not in the HTTP request, so — exactly as the
proxy does — the client's source port is looked up in /proc/net/tcp to
find the owning uid.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import urlsplit

from . import categories as categories_mod
from . import content
from . import pagescan
from . import language
from .urlrules import BLOCK, decide, parse_rules

# Rendered by kosherd (see apply.py). The search service runs unprivileged
# and never reads the policy itself.
SEARCH_POLICY_PATH = Path("/var/lib/kosher-search/policy.json")

SEARCH_BLOCKLIST_PATHS = (
    Path("/var/lib/kosher/search-blocklist.json"),
    Path("/usr/share/kosher/search-blocklist.json"),
)

# A user we know nothing about is filtered as if they had no internet at
# all, the same fail-closed rule the firewall uses for unknown UIDs.
UNKNOWN = {"mode": "none"}

# A query that is plainly asking for help with the problem, rather than for
# the material, is let through — see content.HELP_CONTEXT. Letting it
# through cannot expose anything, because the RESULTS are filtered either
# way, so all this decides is whether the snippets of already-clean sites
# are shown.
HELP_CONTEXT = content.HELP_CONTEXT


def _host_of(url: str) -> str:
    host = urlsplit(url if "//" in url else "//" + url).hostname or ""
    return host.lower().strip(".")


def _under(host: str, domain: str) -> bool:
    """True if host is `domain` or a subdomain of it."""
    domain = domain.lower().strip(".").removeprefix("*.")
    return host == domain or host.endswith("." + domain)


class SearchPolicy:
    """Per-uid policy for the search service, reloaded when the file changes."""

    def __init__(self, path: Path = SEARCH_POLICY_PATH):
        self.path = Path(path)
        self._mtime = None
        self._users: dict[int, dict] = {}

    def for_uid(self, uid: int | None) -> dict:
        self._refresh()
        if uid is None:
            return UNKNOWN
        return self._users.get(uid, UNKNOWN)

    def _refresh(self) -> None:
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            self._users = {}
            return
        if mtime == self._mtime:
            return
        self._mtime = mtime
        try:
            doc = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return
        users = {}
        for key, entry in doc.items():
            try:
                users[int(key)] = entry
            except (TypeError, ValueError):
                continue
        self._users = users


# How much a page may say before it is hidden, per media level. media_level
# is the dial a parent already sets for pictures, and it reads the same way
# for words — somebody who does not want to see immodest images does not
# want to read the page they came from either. "none" still hides outright
# explicit pages, because no mode short of unfiltered wants those.
CONTENT_TOLERANCE = {
    "none": content.NSFW,
    "nsfw": content.NSFW,
    "suggestive": content.SUGGESTIVE,
    "immodest": content.IMMODEST,
    "all": content.IMMODEST,
}


class ResultFilter:
    """Decides whether one search result may be shown to one user."""

    def __init__(self, policy: SearchPolicy | None = None, bundle=None,
                 wordlist=None, blocklist=None, scorer=None, scanner=None):
        self.policy = policy or SearchPolicy()
        self._bundle = bundle
        self._wordlist = wordlist
        self._blocklist = blocklist
        self._scorer = scorer
        self._scanner = scanner

    # Loaded lazily: a 190 MB category database should not be opened by a
    # unit test, nor twice by the search worker processes.
    @property
    def bundle(self):
        if self._bundle is None:
            self._bundle = categories_mod.load_any()
        return self._bundle

    @property
    def wordlist(self):
        if self._wordlist is None:
            self._wordlist = language.load()
        return self._wordlist

    @property
    def blocklist(self):
        if self._blocklist is None:
            self._blocklist = load_blocklist()
        return self._blocklist

    @property
    def scorer(self):
        if self._scorer is None:
            self._scorer = content.load()
        return self._scorer

    @property
    def scanner(self):
        if self._scanner is None:
            self._scanner = pagescan.PageScanner(scorer=self.scorer)
        return self._scanner

    def query_reason(self, uid: int | None, query: str) -> str | None:
        """Why this SEARCH must not run at all, or None to let it run.

        Blocking the query matters as much as blocking the results: an
        explicit search shows explicit text in the result snippets even
        when every link is dropped.
        """
        user = self.policy.for_uid(uid)
        mode = user.get("mode", "none")
        if mode == "unfiltered":
            return None
        if mode == "none":
            return "this account has no internet access"
        if self.blocklist.contains_any(query) and not HELP_CONTEXT.search(query):
            return "that search is blocked"
        if user.get("language_filter", "off") != "off" and \
                self.wordlist.contains_any(query):
            return "that search is blocked"
        return None

    def allows(self, uid: int | None, url: str, *, text: str = "",
               is_media: bool = False) -> bool:
        """True if this result may be shown. `text` is the title + snippet."""
        return self.reason(uid, url, text=text, is_media=is_media) is None

    def reason(self, uid: int | None, url: str, *, text: str = "",
               is_media: bool = False) -> str | None:
        """Why this result must be hidden, or None if it may be shown."""
        user = self.policy.for_uid(uid)
        mode = user.get("mode", "none")
        if mode == "unfiltered":
            return None
        if mode == "none":
            return "no internet access"

        host = _host_of(url)
        if not host:
            return "no host"

        if mode == "whitelist":
            # The point of searching in whitelist mode: show the sites this
            # account can actually open, and nothing else.
            allowed = user.get("whitelist", [])
            if not any(_under(host, d) for d in allowed):
                return "not on the whitelist"
        else:
            blocked = user.get("blocked_categories", [])
            if blocked and self.bundle.blocked_categories_of(host, blocked):
                return "blocked category"

        if mode == "filtered":
            rules = user.get("rules", [])
            if rules:
                try:
                    action, pattern = decide(parse_rules(rules), url)
                except Exception:  # noqa: BLE001 - a bad rule must not open the filter
                    return "unreadable rule"
                if action == BLOCK:
                    return f"blocked by the rule {pattern}"

        if is_media and user.get("media_level", "none") != "none":
            # Thumbnails are pictures chosen by a search engine from pages
            # nobody has vetted. Until the classifier scores them, a user
            # with any media filtering does not get an image search.
            return "images are filtered for this account"

        if text:
            if user.get("language_filter", "off") != "off" and \
                    self.wordlist.contains_any(text):
                return "bad language in the result"
            # The title and snippet are a summary of the page written by
            # the page itself; when they already read as explicit there is
            # no reason to fetch anything.
            tolerance = CONTENT_TOLERANCE.get(user.get("media_level", "none"),
                                              content.NSFW)
            if self.scorer.score(text).at_least(tolerance):
                return "the result reads as inappropriate"
        return None

    def unknown_host(self, url: str) -> bool:
        """True if the category database has nothing to say about this host.

        These are the results worth reading: a host in a known category has
        already been judged, and a host in no category at all is either
        harmless or brand new, which is exactly the gap lists leave open.
        """
        host = _host_of(url)
        if not host:
            return False
        try:
            return not self.bundle.categories_of(host)
        except Exception:  # noqa: BLE001 - a missing database is not a verdict
            return False

    def scan_reason(self, uid: int | None, url: str,
                    verdict=None) -> str | None:
        """Why the FETCHED page must be hidden, given its content verdict."""
        user = self.policy.for_uid(uid)
        mode = user.get("mode", "none")
        if mode in ("unfiltered", "whitelist"):
            # Unfiltered filters nothing; whitelist has already decided by
            # the time we get here, and re-judging an approved site by its
            # words would silently override the person who approved it.
            return None
        if verdict is None:
            return None  # could not read it; the cheap checks stand
        tolerance = CONTENT_TOLERANCE.get(user.get("media_level", "none"),
                                          content.NSFW)
        if verdict.at_least(tolerance):
            return f"the page reads as {verdict.level}"
        return None

    def filter_results(self, uid: int | None, results: list, *,
                       url_of=lambda r: r.get("url", ""),
                       text_of=lambda r: " ".join(
                           str(r.get(k, "")) for k in ("title", "content")),
                       media_of=lambda r: bool(r.get("img_src")
                                               or r.get("thumbnail")),
                       deep: bool = True, max_scans: int = 10) -> list:
        """Filter a page of results: cheap checks first, then read what is left.

        Two passes rather than one because fetching is thousands of times
        more expensive than a list lookup, so it is worth doing only for
        the few results that survive everything cheaper — and only for
        hosts nothing already knows about.
        """
        kept = [r for r in results
                if self.allows(uid, url_of(r), text=text_of(r),
                               is_media=media_of(r))]
        if not deep or not kept:
            return kept
        user = self.policy.for_uid(uid)
        if user.get("mode", "none") in ("unfiltered", "whitelist", "none"):
            return kept

        candidates = []
        for result in kept:
            url = url_of(result)
            if self.unknown_host(url):
                candidates.append((url, _host_of(url)))
            if len(candidates) >= max_scans:
                break
        if not candidates:
            return kept
        verdicts = self.scanner.verdicts(candidates)
        return [r for r in kept
                if self.scan_reason(uid, url_of(r),
                                    verdicts.get(_host_of(url_of(r)))) is None]


def load_blocklist(*paths: Path):
    """Explicit search terms that are blocked outright, in every filtered mode.

    Separate from the profanity list: swearing in a page gets substituted,
    but a search for explicit material should not run at all.
    """
    for path in (paths or SEARCH_BLOCKLIST_PATHS):
        try:
            doc = json.loads(Path(path).read_text())
        except (OSError, ValueError):
            continue
        return language.Wordlist({term: "" for term in doc.get("terms", [])})
    return language.Wordlist()


def render_policy(policy) -> str:
    """Render what the search service needs, per uid, as JSON.

    Everything the service is given, it needs; it is given nothing else —
    no passwords, no app lists, no other users' settings beyond what it
    must filter with.
    """
    per_user = {}
    for user in policy.effective_users():
        per_user[str(user.uid)] = {
            "mode": user.mode,
            "whitelist": list(user.whitelist),
            "blocked_categories": list(user.blocked_categories),
            "rules": user.rules,
            "media_level": user.media_level,
            "language_filter": getattr(user, "language_filter", "off"),
        }
    return json.dumps(per_user, indent=2) + "\n"
