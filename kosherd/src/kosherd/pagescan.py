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
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import content

log = logging.getLogger(__name__)

CACHE_PATH = Path("/var/lib/kosher-search/verdicts.sqlite")

FETCH_TIMEOUT = 2.5      # seconds; a search must not wait longer than this
READ_LIMIT = 128 * 1024  # enough for the head and the top of the body
MAX_PARALLEL = 6
# Long enough that repeat searches are instant, short enough that a
# domain that changes hands is re-judged within a week.
TTL_SECONDS = 7 * 24 * 3600

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
    def __init__(self, path: Path = CACHE_PATH, ttl: int = TTL_SECONDS):
        self.path = Path(path)
        self.ttl = ttl
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
        try:
            with self._lock:
                row = self._conn().execute(
                    "SELECT level, points, seen FROM verdicts WHERE host = ?",
                    (host,)).fetchone()
        except sqlite3.Error:
            return None
        if not row or time.time() - row[2] > self.ttl:
            return None
        return content.Verdict(row[0], row[1], ())

    def put(self, host: str, verdict: content.Verdict) -> None:
        try:
            with self._lock:
                db = self._conn()
                db.execute(
                    "INSERT INTO verdicts (host, level, points, seen) "
                    "VALUES (?, ?, ?, ?) ON CONFLICT(host) DO UPDATE SET "
                    "level=excluded.level, points=excluded.points, seen=excluded.seen",
                    (host, verdict.level, verdict.points, int(time.time())))
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
            return cached
        try:
            html = self._fetch(url)
        except (urllib.error.URLError, OSError, ValueError, TimeoutError):
            return None
        except Exception:  # noqa: BLE001 - a scan must never break a search
            log.debug("scan of %s failed", url, exc_info=True)
            return None
        if not html:
            return None
        verdict = content.score_html(html, self.scorer)
        self.cache.put(host, verdict)
        return verdict

    def verdicts(self, pairs) -> dict[str, content.Verdict | None]:
        """Scan several pages at once. `pairs` is an iterable of (url, host)."""
        pairs = list(pairs)
        if not pairs:
            return {}
        results: dict[str, content.Verdict | None] = {}
        # One thread per page, capped: these are almost all waiting on the
        # network, and the machine this runs on may have two cores.
        with ThreadPoolExecutor(max_workers=min(MAX_PARALLEL, len(pairs))) as pool:
            futures = {pool.submit(self.verdict, url, host): host
                       for url, host in pairs}
            for future, host in futures.items():
                try:
                    results[host] = future.result(timeout=self.timeout + 1)
                except Exception:  # noqa: BLE001
                    results[host] = None
        return results
