"""The search page KosherOS puts in front of the web.

Why a front end of our own rather than a SearXNG plugin:

* Identity. Filtering is per user, and neither SearXNG nor the WSGI layer
  under it reliably exposes the client's source port, which is what tells
  us who is searching. Accepting the connection ourselves does.
* Version churn. SearXNG's plugin API changes between releases; its JSON
  search API does not. Everything version-specific stays in one HTTP call.
* The page itself. In whitelist mode the results page is the only place
  the whitelist is visible, so it has to be ours to design.

SearXNG runs behind this on 127.0.0.1:8889, reachable only by this
service (the firewall rejects human users' connections to that port), so
nobody can fetch unfiltered results by going around us.
"""

from __future__ import annotations

import argparse
import html
import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from kosherd import accessreq
from kosherd import search as search_mod
from kosherd.uidmap import UidLookup

from . import BACKEND, LISTEN_PORT

log = logging.getLogger("kosher-search")

BACKEND_TIMEOUT = 10
RESULTS_PER_PAGE = 20
# SearXNG's own categories, in the order they are offered.
TABS = (("general", "Web"), ("images", "Images"), ("news", "News"),
        ("videos", "Videos"))

# A whitelist account's blocks happen at the firewall, so there is no page
# to put a button on — this is the only place those users can ask for
# anything. Same reserved path as the proxy's block page, for one habit
# rather than two.
REQUEST_PATH = "/request"
MAX_FORM_BYTES = 8192

STYLE = """
:root { color-scheme: light dark;
  --bg:#f6f7fb; --fg:#16203a; --muted:#5b6b8c; --card:#fff; --line:#dfe4f0;
  --accent:#1f4fd8; --accent-fg:#fff; }
@media (prefers-color-scheme: dark) { :root {
  --bg:#0b1a33; --fg:#e8eefc; --muted:#9fb2d8; --card:#111f3d; --line:#22335c;
  --accent:#7ba2ff; --accent-fg:#0b1a33; } }
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--fg); font:16px/1.55
  system-ui, -apple-system, "Cantarell", sans-serif; }
header { position:sticky; top:0; background:var(--bg); border-bottom:1px solid
  var(--line); padding:1rem 1.25rem .5rem; }
.wrap { max-width:46rem; margin:0 auto; }
form { display:flex; gap:.5rem; }
input[type=search] { flex:1; padding:.7rem .9rem; font-size:1rem; border-radius:.6rem;
  border:1px solid var(--line); background:var(--card); color:var(--fg); }
button { padding:.7rem 1.2rem; font-size:1rem; border:0; border-radius:.6rem;
  background:var(--accent); color:var(--accent-fg); cursor:pointer; }
nav { display:flex; gap:.25rem; margin:.6rem 0 0; }
nav a { padding:.35rem .8rem; border-radius:99px; text-decoration:none;
  color:var(--muted); font-size:.9rem; }
nav a.on { background:var(--card); color:var(--fg); border:1px solid var(--line); }
main { padding:1.25rem; }
.result { margin:0 0 1.6rem; }
.result a.title { color:var(--accent); font-size:1.1rem; text-decoration:none; }
.result a.title:hover { text-decoration:underline; }
.result .url { color:var(--muted); font-size:.82rem; word-break:break-all; }
.result p { margin:.25rem 0 0; color:var(--fg); }
.note { background:var(--card); border:1px solid var(--line); border-radius:.8rem;
  padding:1.1rem 1.25rem; color:var(--muted); }
.note strong { color:var(--fg); }
.hidden-count { margin:0 0 1.4rem; color:var(--muted); font-size:.9rem; }
.chips { display:flex; flex-wrap:wrap; gap:.4rem; margin:.75rem 0 0; padding:0;
  list-style:none; }
.chips a { display:inline-block; padding:.3rem .7rem; border-radius:99px;
  border:1px solid var(--line); background:var(--card); text-decoration:none;
  color:var(--fg); font-size:.9rem; }
.pager { display:flex; gap:.75rem; margin-top:2rem; }
.ask { margin-top:1rem; display:flex; gap:.5rem; flex-wrap:wrap; }
.ask input[type=text] { flex:1; min-width:12rem; padding:.55rem .7rem;
  border-radius:.5rem; border:1px solid var(--line); background:var(--bg);
  color:var(--fg); }
.ask button { padding:.55rem 1rem; }
.result form { display:inline; }
.imgrid { display:grid; grid-template-columns:repeat(auto-fill,minmax(160px,1fr));
  gap:.8rem; }
.imgrid a { display:block; border-radius:8px; overflow:hidden;
  background:var(--card); border:1px solid var(--line); text-decoration:none; }
.imgrid img { width:100%; height:130px; object-fit:cover; display:block; }
.imgrid .cap { display:block; padding:.3rem .5rem; font-size:.78rem;
  color:var(--muted); white-space:nowrap; overflow:hidden;
  text-overflow:ellipsis; }
.result .meta { color:var(--muted); font-size:.8rem; margin-top:.15rem; }
.result.media { display:flex; gap:.9rem; }
.result.media img { width:168px; height:96px; object-fit:cover;
  border-radius:8px; background:var(--card); flex:none; }
.result.media .body { min-width:0; }
.result .askbtn { background:none; border:0; color:var(--muted); padding:0;
  font-size:.82rem; cursor:pointer; text-decoration:underline; }
h1 { font-size:1.15rem; margin:0 0 .75rem; }
footer { color:var(--muted); font-size:.8rem; text-align:center; padding:2rem 1rem; }
"""

PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<link rel="search" type="application/opensearchdescription+xml"
      href="/opensearch.xml" title="KosherOS Search">
<style>{style}</style></head>
<body>
<header><div class="wrap">
  <form action="/search" method="get" role="search">
    <input type="search" name="q" value="{query}" autofocus
           placeholder="Search" autocomplete="off" spellcheck="false">
    <input type="hidden" name="category" value="{category}">
    <button type="submit">Search</button>
  </form>
  <nav>{tabs}</nav>
</div></header>
<main><div class="wrap">{body}</div></main>
<footer>KosherOS search &middot; results are filtered for this account</footer>
</body></html>
"""

OPENSEARCH = """<?xml version="1.0" encoding="UTF-8"?>
<OpenSearchDescription xmlns="http://a9.com/-/spec/opensearch/1.1/">
  <ShortName>KosherOS</ShortName>
  <Description>Filtered search on this computer</Description>
  <InputEncoding>UTF-8</InputEncoding>
  <Url type="text/html" method="get"
       template="http://127.0.0.1:{port}/search?q={{searchTerms}}"/>
</OpenSearchDescription>
"""


def esc(text) -> str:
    return html.escape(str(text or ""), quote=True)


class Backend:
    """The local metasearch engine, reached over its JSON API."""

    def __init__(self, base: str = BACKEND, timeout: int = BACKEND_TIMEOUT):
        self.base = base.rstrip("/")
        self.timeout = timeout

    def search(self, query: str, category: str, page: int) -> dict:
        params = urllib.parse.urlencode({
            "q": query,
            "format": "json",
            "categories": category,
            "pageno": max(1, page),
            # Safe search is forced here as well as at DNS level: this is
            # the one request the engines actually see, and a user who
            # reaches the front end must not be able to turn it off.
            "safesearch": 2,
        })
        # SearXNG's bot detection logs an error on every request with no
        # forwarded-for header. There is genuinely no proxy in front of
        # it — we are the only client, on loopback — so state that rather
        # than leave a spurious error in the journal on every search.
        request = urllib.request.Request(f"{self.base}/search?{params}", headers={
            "X-Forwarded-For": "127.0.0.1",
            "X-Real-IP": "127.0.0.1",
        })
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            return json.loads(response.read())


class Handler(BaseHTTPRequestHandler):
    server_version = "kosher-search"
    protocol_version = "HTTP/1.1"

    # Set by serve()
    backend: Backend = None
    result_filter: search_mod.ResultFilter = None
    uids: UidLookup = None
    port: int = LISTEN_PORT

    def log_message(self, fmt, *args):  # noqa: A003 - quieter than the default
        log.debug(fmt, *args)

    # -- plumbing -------------------------------------------------------------

    def _uid(self) -> int | None:
        """The user who opened this connection, from their source port."""
        try:
            peer = self.connection.getpeername()
        except OSError:
            return None
        # The search service is on loopback: the client's local end is its
        # peer address:port, and it connected to us.
        return self.uids.uid_for_connection(peer[0], peer[1],
                                            self.server.server_address[0],
                                            self.server.server_address[1])

    def _send(self, body: str, status: int = 200,
              content_type: str = "text/html; charset=utf-8") -> None:
        raw = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        # Search results are per user and per policy; a cached page could
        # be shown to the next person to sit down.
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def _page(self, body: str, *, query: str = "", category: str = "general",
              title: str = "KosherOS Search", status: int = 200) -> None:
        tabs = "".join(
            f'<a class="{"on" if key == category else ""}" '
            f'href="/search?q={urllib.parse.quote(query)}&amp;category={key}">'
            f"{label}</a>"
            for key, label in TABS) if query else ""
        self._send(PAGE.format(title=esc(title), style=STYLE, query=esc(query),
                               category=esc(category), tabs=tabs, body=body),
                   status=status)

    # -- routes ---------------------------------------------------------------

    def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler's spelling
        parts = urllib.parse.urlsplit(self.path)
        if parts.path != REQUEST_PATH:
            return self._page('<div class="note">Nothing here.</div>', status=404)
        try:
            length = min(int(self.headers.get("Content-Length") or 0),
                         MAX_FORM_BYTES)
        except ValueError:
            length = 0
        form = urllib.parse.parse_qs(self.rfile.read(length).decode(
            "utf-8", "replace"))
        url = (form.get("url") or [""])[0]
        note = (form.get("note") or [""])[0]
        uid = self._uid()
        try:
            if uid is None:
                raise accessreq.RequestError("this computer could not tell "
                                             "who is asking")
            accessreq.submit(uid, url, note)
        except accessreq.RequestError as e:
            body = ('<div class="note"><strong>Could not ask.</strong>'
                    f"<p>{esc(e)}</p></div>")
        except Exception:  # noqa: BLE001 - a failed ask is not a crash
            log.exception("could not record an access request")
            body = ('<div class="note"><strong>Could not ask.</strong>'
                    "<p>Please try again.</p></div>")
        else:
            body = ('<div class="note"><strong>Your request was sent.</strong>'
                    f"<p>The administrator of this computer will see "
                    f"<code>{esc(url)}</code>. Nothing has changed yet.</p>"
                    "</div>")
        return self._page(body, title="Asked — KosherOS Search")

    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler's spelling
        parts = urllib.parse.urlsplit(self.path)
        params = urllib.parse.parse_qs(parts.query)
        if parts.path == "/opensearch.xml":
            return self._send(OPENSEARCH.format(port=self.port),
                              content_type="application/opensearchdescription+xml")
        if parts.path in ("/", "/search"):
            return self._search(params)
        return self._page('<div class="note">Nothing here.</div>', status=404)

    def _search(self, params) -> None:
        query = (params.get("q", [""])[0] or "").strip()
        category = params.get("category", ["general"])[0]
        if category not in dict(TABS):
            category = "general"
        try:
            page = int(params.get("page", ["1"])[0])
        except ValueError:
            page = 1

        uid = self._uid()
        if not query:
            return self._page(self._welcome(uid), category=category)

        refusal = self.result_filter.query_reason(uid, query)
        if refusal:
            return self._page(
                '<div class="note"><strong>This search is blocked.</strong>'
                f"<p>KosherOS did not run it because {esc(refusal)}.</p>"
                "<p>If you need it, ask the administrator of this computer.</p>"
                "</div>",
                query=query, category=category, title="Blocked — KosherOS Search")

        try:
            payload = self.backend.search(query, category, page)
        except (urllib.error.URLError, OSError, ValueError, TimeoutError):
            log.warning("the search backend did not answer", exc_info=True)
            return self._page(
                '<div class="note"><strong>Search is unavailable.</strong>'
                "<p>The search service on this computer is not answering. "
                "It may still be starting up.</p></div>",
                query=query, category=category, status=503)

        results = payload.get("results", [])
        kept = self.result_filter.filter_results(uid, results)
        return self._page(self._results(uid, query, category, page,
                                        results, kept),
                          query=query, category=category,
                          title=f"{query} — KosherOS Search")

    # -- rendering ------------------------------------------------------------

    @staticmethod
    def _ask_form(url: str = "", label: str = "Ask for this") -> str:
        """A form posting to this service's own origin — same-origin, so no
        mixed-content refusal and no scheme to get wrong."""
        return (f'<form class="ask" method="post" action="{REQUEST_PATH}">'
                f'<input type="hidden" name="url" value="{esc(url)}">'
                '<input type="text" name="note" maxlength="200" '
                'placeholder="Why do you need it? (optional)">'
                f"<button type=\"submit\">{esc(label)}</button></form>")

    def _welcome(self, uid) -> str:
        user = self.result_filter.policy.for_uid(uid)
        if user.get("mode") != "whitelist":
            return ('<div class="note">Search the web. Results are filtered '
                    "for this account.</div>")
        # In whitelist mode the whitelist is otherwise invisible: without
        # this, the only way to learn what the computer will open is to
        # guess an address and see whether it loads.
        sites = sorted(user.get("whitelist", []))
        if not sites:
            return ('<div class="note"><strong>No sites are allowed yet.</strong>'
                    "<p>Ask the administrator of this computer to add one:</p>"
                    + self._ask_form(label="Ask") + "</div>")
        chips = "".join(f'<li><a href="https://{esc(s)}/">{esc(s)}</a></li>'
                        for s in sites)
        return ('<div class="note"><strong>Sites you can visit</strong>'
                f'<ul class="chips">{chips}</ul></div>')

    def _results(self, uid, query, category, page, results, kept) -> str:
        if not kept:
            hidden = len(results)
            extra = ("" if not hidden else
                     f"<p>{hidden} result(s) were hidden by the filter for "
                     "this account.</p>")
            return ('<div class="note"><strong>No results to show.</strong>'
                    f"{extra}<p>Try different words, or ask the administrator "
                    "of this computer for a site:</p>"
                    + self._ask_form(label="Ask") + "</div>")

        blocks = []
        hidden = len(results) - len(kept)
        if hidden:
            blocks.append(f'<p class="hidden-count">{hidden} result(s) hidden '
                          "by the filter for this account.</p>")
        # Each tab renders like the kind of thing it holds. All of these
        # used to render as the general text list, which made the Images,
        # News and Videos tabs look broken — a wall of bare links where a
        # person expects pictures and thumbnails. The thumbnails themselves
        # are fetched by the browser through the proxy, so the account's
        # media filtering applies to them exactly as everywhere else.
        if category == "images":
            cells = []
            for result in kept:
                src = result.get("thumbnail_src") or result.get("img_src") or ""
                if not src or src.startswith("data:"):
                    continue
                url = result.get("url", "")
                title = result.get("title") or url
                cells.append(
                    f'<a href="{esc(url)}">'
                    f'<img src="{esc(src)}" alt="{esc(title)}" loading="lazy">'
                    f'<span class="cap">{esc(title)}</span></a>')
            blocks.append(f'<div class="imgrid">{"".join(cells)}</div>')
        else:
            for result in kept:
                url = result.get("url", "")
                title = result.get("title") or url
                snippet = result.get("content") or ""
                meta = []
                published = (result.get("publishedDate") or "")[:10]
                if published:
                    meta.append(esc(published))
                if result.get("length"):
                    meta.append(esc(result["length"]))
                if result.get("author"):
                    meta.append(esc(result["author"]))
                meta_html = (f'<div class="meta">{" · ".join(meta)}</div>'
                             if meta else "")
                thumb = result.get("thumbnail") or result.get("thumbnail_src") or ""
                if category == "videos" and thumb and not thumb.startswith("data:"):
                    blocks.append(
                        '<div class="result media">'
                        f'<img src="{esc(thumb)}" alt="" loading="lazy">'
                        '<div class="body">'
                        f'<a class="title" href="{esc(url)}">{esc(title)}</a>'
                        f'<div class="url">{esc(url)}</div>{meta_html}'
                        f"<p>{esc(snippet)}</p></div></div>")
                else:
                    blocks.append(
                        '<div class="result">'
                        f'<a class="title" href="{esc(url)}">{esc(title)}</a>'
                        f'<div class="url">{esc(url)}</div>{meta_html}'
                        f"<p>{esc(snippet)}</p></div>")

        pager = []
        if page > 1:
            pager.append(self._page_link(query, category, page - 1, "Previous"))
        if len(results) >= RESULTS_PER_PAGE:
            pager.append(self._page_link(query, category, page + 1, "Next"))
        if pager:
            blocks.append('<div class="pager">' + "".join(pager) + "</div>")
        blocks.append(self._askable(uid, results))
        return "".join(blocks)

    def _askable(self, uid, results) -> str:
        """Sites a whitelist user could ask to have added.

        Without this, whitelist mode can only shrink: the account cannot
        see what it is missing, so it cannot ask for it, so the whitelist
        never grows past what an admin thought of in advance. Only the
        host is shown — never a title or a snippet from a hidden result.
        """
        hosts = self.result_filter.askable_hosts(uid, results)
        if not hosts:
            return ""
        rows = "".join(
            '<form class="ask" method="post" action="' + REQUEST_PATH + '">'
            f'<input type="hidden" name="url" value="https://{esc(h)}/">'
            f"<span style=\"flex:1\">{esc(h)}</span>"
            '<button type="submit">Ask for this site</button></form>'
            for h in hosts)
        return ('<div class="note" style="margin-top:2rem">'
                "<strong>Other sites matched</strong>"
                "<p>These are not on this account's list yet. You can ask "
                "the administrator of this computer to add one.</p>"
                f"{rows}</div>")

    @staticmethod
    def _page_link(query, category, page, label) -> str:
        params = urllib.parse.urlencode({"q": query, "category": category,
                                         "page": page})
        return f'<a href="/search?{params}">{esc(label)}</a>'


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def serve(port: int = LISTEN_PORT, backend: str = BACKEND) -> None:
    Handler.backend = Backend(backend)
    Handler.result_filter = search_mod.ResultFilter()
    Handler.uids = UidLookup()
    Handler.port = port
    # Loopback only. The firewall keeps other machines out too, but a
    # service that never listens outward cannot be exposed by mistake.
    with Server(("127.0.0.1", port), Handler) as httpd:
        log.info("kosher-search listening on 127.0.0.1:%d", port)
        httpd.serve_forever()


def main() -> int:
    parser = argparse.ArgumentParser(description="KosherOS search front end")
    parser.add_argument("--port", type=int, default=LISTEN_PORT)
    parser.add_argument("--backend", default=BACKEND)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(message)s")
    serve(args.port, args.backend)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
