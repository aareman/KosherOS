"""Fetching and scoring pages the domain lists have never heard of.

The category database knows ten million hosts. The web has far more, and
the ones a filter most needs to judge are exactly the ones nobody has
catalogued yet — a domain registered last week, a free-hosting subdomain,
a search result from a site with no reputation either way.

For those, KosherOS reads the page itself. The scan runs on the device
(no query leaves the house beyond the fetch that a click would have made
anyway), reads only the first 128 KB — the title, the description and the
opening of the body carry the character of a page — and gives up after a
couple of seconds so a search never hangs on a slow site.

Verdicts are cached on disk, because the expensive part is the fetch and
the same handful of domains come up over and over. The cache is keyed by
host, not URL: a host that serves explicit material on one page is not
somewhere we need to check page by page.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, wait
from pathlib import Path

from . import content

log = logging.getLogger(__name__)

CACHE_PATH = Path("/var/lib/kosher-search/verdicts.sqlite")

# How long a search waits for the scan, in wall-clock seconds for the
# WHOLE batch. The same number is the socket timeout of each fetch: a
# single read slower than the entire budget cannot make the page in time.
#
# It used to be a per-operation timeout only, and the page waited for the
# last fetch to finish. DNS, each redirect hop and every read got their
# own allowance, the results were collected one at a time with a fresh
# wait each, and the pool was joined before the page went out — so a
# search sat for the slowest site, and on a home connection that was
# often most of ten seconds. Now the budget is a deadline and nothing on
# the page waits past it.
FETCH_TIMEOUT = 2.0
READ_LIMIT = 128 * 1024  # enough for the head and the top of the body
# One thread per candidate, so a batch is one round rather than two:
# these are all waiting on the network, and ten sleeping threads cost
# nothing a two-core machine notices.
MAX_PARALLEL = 10
# Long enough that repeat searches are instant, short enough that a
# domain that changes hands is re-judged within a week.
TTL_SECONDS = 7 * 24 * 3600
# A host that could not be read is remembered too, for this long. It is
# not a verdict — the result is still shown — but without it a site that
# answers scrapers by hanging, or serves nothing readable, was fetched
# again on every search that listed it, and each time cost the whole
# budget. An hour is long enough to pay that once rather than every
# search, short enough that a site that was merely down is tried again.
RETRY_SECONDS = 3600
# The level stored for such a host. Never returned as a verdict.
UNREADABLE = "unreadable"

USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) KosherOS-scan/1.0"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS verdicts (
    host   TEXT PRIMARY KEY,
    level  TEXT NOT NULL,
    points INTEGER NOT NULL,
    seen   INTEGER NOT NULL
);
"""


class VerdictCache:
    def __init__(self, path: Path = CACHE_PATH, ttl: int = TTL_SECONDS,
                 retry: int = RETRY_SECONDS):
        self.path = Path(path)
        self.ttl = ttl
        self.retry = retry
        self._lock = threading.Lock()
        self._db = None

    def _conn(self):
        if self._db is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._db = sqlite3.connect(self.path, check_same_thread=False)
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.executescript(_SCHEMA)
        return self._db

    def get(self, host: str) -> content.Verdict | None:
        """The cached verdict, or one at level UNREADABLE for a host that
        recently could not be read, or None for a host to fetch."""
        try:
            with self._lock:
                row = self._conn().execute(
                    "SELECT level, points, seen FROM verdicts WHERE host = ?",
                    (host,)).fetchone()
        except sqlite3.Error:
            return None
        if not row:
            return None
        ttl = self.retry if row[0] == UNREADABLE else self.ttl
        if time.time() - row[2] > ttl:
            return None
        return content.Verdict(row[0], row[1], ())

    def put(self, host: str, verdict: content.Verdict) -> None:
        self._store(host, verdict.level, verdict.points)

    def remember_unreadable(self, host: str) -> None:
        """Note that this host could not be read, so it is not tried again
        on every search for the next RETRY_SECONDS."""
        self._store(host, UNREADABLE, 0)

    def _store(self, host: str, level: str, points: int) -> None:
        try:
            with self._lock:
                db = self._conn()
                db.execute(
                    "INSERT INTO verdicts (host, level, points, seen) "
                    "VALUES (?, ?, ?, ?) ON CONFLICT(host) DO UPDATE SET "
                    "level=excluded.level, points=excluded.points, seen=excluded.seen",
                    (host, level, points, int(time.time())))
                db.commit()
        except sqlite3.Error:
            log.debug("could not cache a verdict for %s", host, exc_info=True)


class PageScanner:
    def __init__(self, scorer: content.Scorer | None = None,
                 cache: VerdictCache | None = None,
                 fetch=None, timeout: float = FETCH_TIMEOUT):
        self._scorer = scorer
        self.cache = cache if cache is not None else VerdictCache()
        self.timeout = timeout
        self._fetch = fetch or self._http_get

    @property
    def scorer(self) -> content.Scorer:
        if self._scorer is None:
            self._scorer = content.load()
        return self._scorer

    def _http_get(self, url: str) -> str:
        request = urllib.request.Request(url, headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml",
            # Ask for the top of the page; servers that ignore it are cut
            # off by the read limit below anyway.
            "Range": f"bytes=0-{READ_LIMIT - 1}",
        })
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            kind = response.headers.get_content_type()
            if kind not in ("text/html", "application/xhtml+xml", "text/plain"):
                return ""
            raw = response.read(READ_LIMIT)
        charset = response.headers.get_content_charset() or "utf-8"
        return raw.decode(charset, "replace")

    def verdict(self, url: str, host: str) -> content.Verdict | None:
        """Score one page. None means we could not tell (fetch failed)."""
        cached = self.cache.get(host)
        if cached is not None:
            return None if cached.level == UNREADABLE else cached
        try:
            html = self._fetch(url)
        except (urllib.error.URLError, OSError, ValueError, TimeoutError):
            self.cache.remember_unreadable(host)
            return None
        except Exception:  # noqa: BLE001 - a scan must never break a search
            log.debug("scan of %s failed", url, exc_info=True)
            self.cache.remember_unreadable(host)
            return None
        if not html:
            self.cache.remember_unreadable(host)
            return None
        verdict = content.score_html(html, self.scorer)
        self.cache.put(host, verdict)
        return verdict

    def verdicts(self, pairs, budget: float | None = None
                 ) -> dict[str, content.Verdict | None]:
        """Scan several pages at once. `pairs` is an iterable of (url, host).

        Returns within `budget` seconds (the fetch timeout by default)
        whatever has been decided by then. A page still loading at the
        deadline counts as "could not tell", exactly like one that failed:
        the cheap checks stand and the result is shown. Its fetch is not
        abandoned, though — it finishes on its own thread and its verdict
        goes into the cache, so the NEXT search that turns up the same
        host is judged instantly. A slow site is checked on the second
        look rather than never, and no search waits for it.
        """
        pairs = list(pairs)
        if not pairs:
            return {}
        if budget is None:
            budget = self.timeout
        results: dict[str, content.Verdict | None] = {}
        pool = ThreadPoolExecutor(max_workers=min(MAX_PARALLEL, len(pairs)))
        futures = {pool.submit(self.verdict, url, host): host
                   for url, host in pairs}
        done, _ = wait(futures, timeout=budget)
        for future, host in futures.items():
            if future not in done:
                results[host] = None
                continue
            try:
                results[host] = future.result()
            except Exception:  # noqa: BLE001 - a scan must never break a search
                results[host] = None
        # Not `with`: the context manager joins every worker, which is the
        # wait this method exists to avoid. The stragglers keep running.
        pool.shutdown(wait=False)
        return results
