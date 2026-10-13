"""Fetching and scoring pages the domain lists have never heard of.

The category database knows ten million hosts. The web has far more, and
the ones a filter most needs to judge are exactly the ones nobody has
catalogued yet — a domain registered last week, a free-hosting subdomain,
a search result from a site with no reputation either way.

For those, KosherOS reads the page itself. The scan runs on the device
(no query leaves the house beyond the fetch that a click would have made
anyway) and reads only the first 128 KB — the title, the description and
the opening of the body carry the character of a page.

It runs AFTER the page is sent, never before. A search shows what the
cheap checks allow and what the cache already knows; the hosts nobody
has judged are read in the background, and the verdict applies from the
next search on. Waiting for the fetch made every fresh search as slow as
the slowest site in it, for a protection the filtering proxy gives
anyway: in filtered mode it reads the page itself when the link is
clicked, and the title and snippet are scored before anything is shown.

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
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import content

log = logging.getLogger(__name__)

CACHE_PATH = Path("/var/lib/kosher-search/verdicts.sqlite")

# Socket timeout of a fetch. Nothing waits on it, so it can afford to be
# generous: a slow site that does answer is worth more than a fast "could
# not tell".
FETCH_TIMEOUT = 5.0
READ_LIMIT = 128 * 1024  # enough for the head and the top of the body
# Background readers. They are all waiting on the network, so this is not
# a number of cores; it is how many sites are asked at once.
MAX_PARALLEL = 6
# How many hosts may be queued for reading before new ones are dropped on
# the floor. A search queues at most ten, and each read finishes within a
# few socket timeouts, so this only bites when the network has gone away
# — and then there is nothing to learn anyway.
MAX_PENDING = 40
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
        self._pool = None
        self._pending: set[str] = set()
        self._lock = threading.Lock()

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

    def known_verdicts(self, pairs) -> dict[str, content.Verdict | None]:
        """What the cache already says about these (url, host) pairs.

        Returns at once. A host with no cached verdict is None here and
        is queued to be read in the background; its verdict is in the
        cache by the time the next search lists it. A host already being
        read is not queued twice.
        """
        results: dict[str, content.Verdict | None] = {}
        to_read = []
        for url, host in pairs:
            cached = self.cache.get(host)
            if cached is None:
                results[host] = None
                to_read.append((url, host))
            else:
                results[host] = None if cached.level == UNREADABLE else cached
        if to_read:
            self.read_later(to_read)
        return results

    def read_later(self, pairs) -> None:
        """Fetch and judge these pages in the background."""
        with self._lock:
            if self._pool is None:
                self._pool = ThreadPoolExecutor(
                    max_workers=MAX_PARALLEL, thread_name_prefix="pagescan")
            for url, host in pairs:
                if host in self._pending or len(self._pending) >= MAX_PENDING:
                    continue
                self._pending.add(host)
                self._pool.submit(self._read, url, host)

    def _read(self, url: str, host: str) -> None:
        try:
            self.verdict(url, host)
        except Exception:  # noqa: BLE001 - a background read must die quietly
            log.debug("background scan of %s failed", url, exc_info=True)
        finally:
            with self._lock:
                self._pending.discard(host)

    def idle(self, timeout: float = 5.0) -> bool:
        """True once nothing is being read. Mostly for tests."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                if not self._pending:
                    return True
            time.sleep(0.02)
        return False
