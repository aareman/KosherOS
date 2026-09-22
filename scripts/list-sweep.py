#!/usr/bin/env python3
"""Run the shipped word lists over real pages, the way the proxy does.

The unit tests check the machinery and a sweep over sentences checks the
lists; neither can tell you that "af" is Dutch or that a news portal's
front page quotes a politician's language. Only real pages do that. This
fetches them — a lot of them — judges each with the lists exactly as the
filtering proxy would, and says what the lists would have done to a
family's ordinary reading and to the sites they are meant to catch.

Three groups of sites:

* **curated** — ordinary sites a family in each language actually reads
  (news, shopping, government, cooking, Torah), listed by hand in
  os-image/lists/sweep/sites.json. Nothing here should be blocked for
  its words, and nothing on it should be rewritten.
* **popular** — the top of the Tranco ranking of the most visited
  domains on the web, minus anything the UT1 adult category lists. Not
  curated, so some of it IS suggestive (tabloids, dating sites, fashion),
  which is why its threshold is looser. It is here for breadth: a
  thousand pages in the languages people actually use, chosen by nobody
  on this project.
* **adult** — a sample of the UT1 adult category, fetched at run time. The
  domain lists block these outright; the word lists are the backstop for
  the sites nobody has catalogued, and this measures how good a backstop
  they are. No adult domain is written into this repository, printed in a
  log, or put in the summary — a miss is named only in the artifact.

Each fetched page gets the proxy's treatment (kosher_filter._filter_page):
the first 200 KB, the visible text, the page scorer's verdict and the
bad-language list's hits. Then thresholds: a page that should be clean and
reads as explicit is a false positive, a page that should be caught and
reads as clean is a miss, and too many of either fails the run. Sites
that could not be fetched are counted, not judged — a runner without
network must not make the lists look wrong — and a group with too few
fetched pages to mean anything is reported without a verdict.

    python3 scripts/list-sweep.py                     # everything
    python3 scripts/list-sweep.py --curated-only      # no downloads
    python3 scripts/list-sweep.py --popular 300 --adult 100

Writes a Markdown report to --summary (GITHUB_STEP_SUMMARY in CI) and the
per-page detail to --out as JSON.
"""

from __future__ import annotations

import argparse
import gzip
import io
import json
import random
import re
import sys
import tarfile
import time
import urllib.error
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "kosherd" / "src"))
from kosherd import content, language  # noqa: E402

SHIPPED = ROOT / "os-image" / "files" / "usr" / "share" / "kosher"
CURATED_PATH = ROOT / "os-image" / "lists" / "sweep" / "sites.json"

TRANCO_URL = "https://tranco-list.eu/top-1m.csv.zip"
ADULT_URL = "https://dsi.ut-capitole.fr/blacklists/download/adult.tar.gz"

# What the proxy reads of a page (kosher_filter.MAX_SCORED_BYTES).
SCORED_BYTES = 200_000
# A response smaller than this is a redirect stub, a bot wall or an error
# page, not a front page; it is counted as unfetched.
MIN_PAGE_BYTES = 2_000
FETCH_TIMEOUT = 20
USER_AGENT = ("Mozilla/5.0 (X11; Linux x86_64; rv:130.0) "
              "Gecko/20100101 Firefox/130.0")

# The thresholds. A group with fewer fetched pages than MIN_JUDGED gets a
# report and no verdict, because five pages prove nothing either way. A
# language's own check gets a lower bar, since there are a dozen curated
# sites per language and a third of them being rewritten is a finding.
MIN_JUDGED = 20
MIN_JUDGED_PER_LANGUAGE = 8
# Curated pages that read as explicit or suggestive. Some of these are
# real — a news front page reporting on pornography — which is why it is
# a rate and not zero. Measured at 3% when the lists shipped.
CURATED_MAX_EXPLICIT = 0.06
# Curated pages on which the bad-language list would rewrite something,
# per language. News sites in some languages quote language freely
# (Hungarian portals measured at a third), so this only catches a list
# that rewrites ordinary words wholesale.
CURATED_MAX_REWRITTEN = 0.5
# Popular sites that read as EXPLICIT (nsfw), which the top of the web,
# minus the adult category, should not.
POPULAR_MAX_NSFW = 0.03
# Adult pages the scorer reads as explicit. A word scorer cannot read a
# page that is a script and no text, or a parked domain, and a fair share
# of the adult category is one or the other — the domain lists are what
# catch those. The first CI run fetched 227 of 400 sampled domains and
# read 63% of them as explicit; most of the rest had no listed word on
# them at all. Set to sit a little under that, and to be raised as the
# lists improve.
ADULT_MIN_RECALL = 0.50

ACCEPT_LANGUAGE = {
    "en": "en-US,en;q=0.9", "he": "he-IL,he;q=0.9,en;q=0.5", "yi": "yi,he;q=0.8",
    "ru": "ru-RU,ru;q=0.9", "uk": "uk-UA,uk;q=0.9", "fr": "fr-FR,fr;q=0.9",
    "es": "es-ES,es;q=0.9", "pt": "pt-BR,pt;q=0.9", "de": "de-DE,de;q=0.9",
    "it": "it-IT,it;q=0.9", "nl": "nl-NL,nl;q=0.9", "hu": "hu-HU,hu;q=0.9",
    "pl": "pl-PL,pl;q=0.9", "ar": "ar-SA,ar;q=0.9", "fa": "fa-IR,fa;q=0.9",
    "tr": "tr-TR,tr;q=0.9",
}


@dataclass
class Page:
    group: str            # curated | popular | adult
    language: str         # curated only; "" otherwise
    url: str
    status: str = ""      # "ok", or why it was not judged
    size: int = 0
    level: str = ""
    points: int = 0
    hits: list[str] = field(default_factory=list)
    words: list[str] = field(default_factory=list)

    @property
    def judged(self) -> bool:
        return self.status == "ok"


# -- fetching ------------------------------------------------------------------

def fetch(url: str, accept_language: str = "en-US,en;q=0.9") -> tuple[str, str, int]:
    """(status, html, size). Status is "ok" or a short reason."""
    request = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.5",
        "Accept-Language": accept_language,
        "Accept-Encoding": "gzip",
    })
    try:
        with urllib.request.urlopen(request, timeout=FETCH_TIMEOUT) as response:
            raw = response.read(2_000_000)
            if response.headers.get("Content-Encoding", "").lower() == "gzip":
                try:
                    raw = gzip.decompress(raw)
                except OSError:
                    pass
            content_type = response.headers.get("Content-Type", "")
    except urllib.error.HTTPError as e:
        return f"http {e.code}", "", 0
    except Exception as e:  # noqa: BLE001 - every failure is the same to us: not judged
        return type(e).__name__, "", 0
    if "html" not in content_type.lower() and b"<html" not in raw[:2000].lower():
        return "not html", "", len(raw)
    if len(raw) < MIN_PAGE_BYTES:
        return "too small", "", len(raw)
    return "ok", decode(raw, content_type), len(raw)


def decode(raw: bytes, content_type: str) -> str:
    """The page as text, by its declared charset, else UTF-8, else Latin-1.

    The proxy has mitmproxy for this; here it is done by hand, and the
    fallbacks matter because a Hebrew or Russian page in a legacy charset
    read as UTF-8 is a page of replacement characters that matches nothing.
    """
    m = re.search(r"charset=[\"']?([\w-]+)", content_type, re.I)
    charset = m.group(1) if m else None
    if not charset:
        m = re.search(rb"<meta[^>]+charset=[\"']?([\w-]+)", raw[:5000], re.I)
        charset = m.group(1).decode() if m else None
    for cs in (charset, "utf-8"):
        if not cs:
            continue
        try:
            return raw.decode(cs)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("latin-1")


# -- judging ---------------------------------------------------------------------

class Judge:
    """The lists as shipped, applied as the proxy applies them."""

    def __init__(self, shipped: Path = SHIPPED):
        self.scorer = content.load(shipped / "content-terms.json")
        self.wordlist = language.load(shipped / "wordlist.json")

    def judge(self, page: Page, html: str) -> None:
        text = content.visible_text(html[:SCORED_BYTES])
        verdict = self.scorer.score(text)
        page.level, page.points, page.hits = verdict.level, verdict.points, list(verdict.hits)
        page.words = self.wordlist.words_in(text)


def run(pages: list[Page], judge: Judge, workers: int) -> None:
    def one(page: Page) -> None:
        status, html, size = fetch(page.url, ACCEPT_LANGUAGE.get(page.language, "en-US,en;q=0.9"))
        page.status, page.size = status, size
        if status == "ok":
            judge.judge(page, html)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(one, pages))


# -- the three groups ----------------------------------------------------------------

def curated_pages() -> list[Page]:
    sites = json.loads(CURATED_PATH.read_text())
    return [Page("curated", lang, url) for lang, urls in sites.items() for url in urls]


def download(url: str, what: str) -> bytes | None:
    try:
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=180) as response:
            return response.read()
    except Exception as e:  # noqa: BLE001 - reported, and that group is skipped
        print(f"could not download {what}: {e}", file=sys.stderr)
        return None


def adult_domains() -> set[str] | None:
    """Every domain in the UT1 adult category, or None if it could not be had."""
    data = download(ADULT_URL, "the UT1 adult category")
    if data is None:
        return None
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        name = next((m for m in tar.getnames() if m.endswith("/domains")), None)
        member = tar.extractfile(name) if name else None
        if member is None:
            return None
        return {line.decode("utf-8", "ignore").strip().lower() for line in member} - {""}


def popular_pages(count: int, exclude: set[str]) -> list[Page]:
    """The top of the Tranco list, without the adult category, as https URLs.

    Takes more than `count` off the top and drops what is obviously not a
    page — CDN and API hosts, whose root is an error — by name; what is
    left after fetching decides itself by being HTML or not.
    """
    data = download(TRANCO_URL, "the Tranco list")
    if data is None:
        return []
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        csv = archive.read(archive.namelist()[0]).decode("utf-8", "ignore")
    skip = re.compile(r"(^|\.)(cdn|static|api|img|images|assets|ajax|edge|akamai|"
                      r"cloudfront|fastly|akadns|gstatic|googleapis|googlevideo|"
                      r"doubleclick|googlesyndication|ytimg|fbcdn|twimg|"
                      r"windowsupdate|digicert|letsencrypt|amazonaws|azure|"
                      r"cloudflare|akamaized|llnwd|edgekey|edgesuite|root-servers|"
                      r"in-addr|apple-dns|icloud-content|office365|sharepoint)(\.|$)")
    pages: list[Page] = []
    for line in csv.splitlines():
        _rank, _, domain = line.strip().partition(",")
        domain = domain.strip().lower()
        if not domain or domain in exclude or skip.search(domain):
            continue
        pages.append(Page("popular", "", f"https://{domain}/"))
        if len(pages) >= count:
            break
    return pages


def adult_pages(count: int, domains: set[str], seed: int) -> list[Page]:
    """A sample of the adult category. Many of these are dead; ask for more
    than you need and let the fetch decide."""
    picked = random.Random(seed).sample(sorted(domains), min(count, len(domains)))
    return [Page("adult", "", f"https://{d}/") for d in picked]


# -- the report --------------------------------------------------------------------

def explicit(page: Page) -> bool:
    return page.level in (content.NSFW, content.SUGGESTIVE)


@dataclass
class Check:
    name: str
    judged: int
    count: int
    rate: float
    limit: float
    kind: str        # "max" or "min"
    minimum: int = MIN_JUDGED

    @property
    def verdict(self) -> str:
        if self.judged < self.minimum:
            return "no verdict"
        ok = self.rate <= self.limit if self.kind == "max" else self.rate >= self.limit
        return "pass" if ok else "FAIL"

    @property
    def failed(self) -> bool:
        return self.verdict == "FAIL"


def checks(pages: list[Page]) -> list[Check]:
    out: list[Check] = []
    curated = [p for p in pages if p.group == "curated" and p.judged]
    n = len(curated)
    bad = sum(explicit(p) for p in curated)
    out.append(Check("curated pages reading as explicit or suggestive", n, bad,
                     bad / n if n else 0.0, CURATED_MAX_EXPLICIT, "max"))
    for lang in sorted({p.language for p in curated}):
        mine = [p for p in curated if p.language == lang]
        rewritten = sum(bool(p.words) for p in mine)
        out.append(Check(f"curated {lang} pages the language filter would rewrite",
                         len(mine), rewritten, rewritten / len(mine), CURATED_MAX_REWRITTEN, "max",
                         minimum=MIN_JUDGED_PER_LANGUAGE))
    popular = [p for p in pages if p.group == "popular" and p.judged]
    n = len(popular)
    bad = sum(p.level == content.NSFW for p in popular)
    out.append(Check("popular sites reading as explicit", n, bad,
                     bad / n if n else 0.0, POPULAR_MAX_NSFW, "max"))
    adult = [p for p in pages if p.group == "adult" and p.judged]
    n = len(adult)
    caught = sum(p.level == content.NSFW for p in adult)
    out.append(Check("adult pages the scorer reads as explicit", n, caught,
                     caught / n if n else 0.0, ADULT_MIN_RECALL, "min"))
    return out


def host(url: str) -> str:
    return urlsplit(url).hostname or url


def summary(pages: list[Page], results: list[Check], elapsed: float) -> str:
    """The Markdown report. Curated and popular pages are named — they are
    ordinary sites — and adult pages never are."""
    lines = ["# List sweep", ""]
    by_group = {}
    for p in pages:
        by_group.setdefault(p.group, []).append(p)
    lines.append("| group | sites | fetched | explicit | suggestive | immodest | clean | rewritten |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for group in ("curated", "popular", "adult"):
        ps = by_group.get(group, [])
        judged = [p for p in ps if p.judged]
        levels = {lv: sum(p.level == lv for p in judged) for lv in content.LEVELS}
        lines.append(f"| {group} | {len(ps)} | {len(judged)} | {levels[content.NSFW]} | "
                     f"{levels[content.SUGGESTIVE]} | {levels[content.IMMODEST]} | "
                     f"{levels[content.CLEAN]} | {sum(bool(p.words) for p in judged)} |")
    lines += ["", f"Fetched in {elapsed:.0f}s. Unfetched pages are counted, not judged.", ""]

    lines += ["## Checks", "", "| check | pages | count | rate | limit | verdict |", "|---|---|---|---|---|---|"]
    for c in results:
        sign = "≤" if c.kind == "max" else "≥"
        lines.append(f"| {c.name} | {c.judged} | {c.count} | {c.rate:.1%} | {sign} {c.limit:.0%} | {c.verdict} |")
    lines.append("")

    flagged = [p for p in pages if p.group in ("curated", "popular") and p.judged
               and (explicit(p) or p.level == content.IMMODEST or p.words)]
    if flagged:
        lines += ["## Ordinary pages the lists reacted to", "",
                  "Each is either the filter reading real content (a news story, a swimwear "
                  "department) or an entry that is an ordinary word somewhere; the words say which.", "",
                  "| group | lang | site | verdict | points | terms | words |", "|---|---|---|---|---|---|---|"]
        for p in sorted(flagged, key=lambda p: (p.group, p.language, -p.points)):
            lines.append(f"| {p.group} | {p.language or '-'} | {host(p.url)} | {p.level} | {p.points} | "
                         f"{', '.join(p.hits[:5])} | {', '.join(p.words[:5])} |")
        lines.append("")

    adult = by_group.get("adult", [])
    judged = [p for p in adult if p.judged]
    if judged:
        missed = [p for p in judged if p.level != content.NSFW]
        lines += ["## Adult sample", "",
                  f"{len(judged)} of {len(adult)} sampled domains answered with a page; "
                  f"{len(judged) - len(missed)} read as explicit, {len(missed)} did not. "
                  "The ones that did not are listed in the artifact, not here.", ""]
        unfetched = {}
        for p in adult:
            if not p.judged:
                unfetched[p.status] = unfetched.get(p.status, 0) + 1
        if unfetched:
            lines.append("Unfetched: " + ", ".join(f"{k} ×{v}" for k, v in sorted(unfetched.items(), key=lambda kv: -kv[1])[:8]) + ".")
            lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--popular", type=int, default=800,
                        help="how many of the most visited domains to fetch (0 to skip)")
    parser.add_argument("--adult", type=int, default=400,
                        help="how many adult-category domains to sample (0 to skip)")
    parser.add_argument("--curated-only", action="store_true",
                        help="only the hand-listed sites; nothing downloaded")
    parser.add_argument("--seed", type=int, default=None,
                        help="seed for the adult sample (default: the week number, so a "
                             "weekly run sees new sites and a rerun sees the same ones)")
    parser.add_argument("--workers", type=int, default=24)
    parser.add_argument("--summary", type=Path, default=None,
                        help="write the Markdown report here (appended, for GITHUB_STEP_SUMMARY)")
    parser.add_argument("--out", type=Path, default=None,
                        help="write per-page JSON detail into this directory")
    args = parser.parse_args()

    started = time.time()
    judge = Judge()
    pages = curated_pages()
    if not args.curated_only:
        adult = adult_domains() if (args.adult or args.popular) else None
        if args.popular:
            pages += popular_pages(args.popular, adult or set())
        if args.adult and adult:
            seed = args.seed if args.seed is not None else int(time.strftime("%Y%W"))
            pages += adult_pages(args.adult, adult, seed)
    print(f"fetching {len(pages)} pages with {args.workers} workers...", flush=True)
    run(pages, judge, args.workers)
    elapsed = time.time() - started

    results = checks(pages)
    report = summary(pages, results, elapsed)
    print(report)
    if args.summary:
        with open(args.summary, "a", encoding="utf-8") as f:
            f.write(report + "\n")
    if args.out:
        args.out.mkdir(parents=True, exist_ok=True)
        (args.out / "pages.json").write_text(json.dumps([asdict(p) for p in pages], indent=1, ensure_ascii=False))
        (args.out / "checks.json").write_text(json.dumps([asdict(c) | {"verdict": c.verdict} for c in results], indent=1))

    failed = [c for c in results if c.failed]
    for c in failed:
        print(f"FAIL: {c.name}: {c.count}/{c.judged} = {c.rate:.1%}", file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
