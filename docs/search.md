# Search

## Why the OS ships a search engine

A filter that only blocks pages makes searching hard. Every search
returns ten links, some of which lead to a block page, and the person has
to guess which. In approved-sites mode it is harder: the list is invisible,
so there is no way to find out what the computer will open short of typing
addresses and seeing what happens.

So KosherOS filters the results with the same policy that filters the
traffic. A filtered account never sees a link into a blocked category. An
approved-sites account searching for "kosher recipes" gets back the
approved sites that match, which makes the list browsable for the first
time.

## The pieces

    browser ──▶ kosher-search (127.0.0.1:8888) ──▶ SearXNG (127.0.0.1:8889) ──▶ engines
                  │  who is asking
                  │  may this query run?
                  │  may this result be shown?
                  └─ read the page if nothing knows it

The KosherOS front end is a small standard-library HTTP server. It accepts
the connection itself, which is the only reliable way to learn the
client's source port and therefore which account is searching. Filtering
is per account, so that identity is the whole point.

A results page is sent in two pieces. The head and the search bar go out
the moment the query arrives, so the browser draws them at once, and the
results follow in the same response when the engine has answered and the
filter has checked them. A “Searching…” status appears during that wait.

SearXNG sits behind it and does the one job that needs a large dependency:
scraping a dozen search engines that keep changing. It is reached only
over its JSON API, which is stable, so a SearXNG upgrade cannot reach the
filtering. Its plugin API, which changes between releases, is not used.

The firewall refuses any human account's connection to SearXNG's own port.
Loopback is otherwise open, which is what lets a person reach the search
page, and the engine's raw results carry exactly the snippets the filter
exists to withhold.

## Response time

The front end prepares its shared filtering data at service startup. Compiling
the language matchers and opening the category database no longer delays the
first person's search.

Successful, nonempty engine responses stay in memory for 60 seconds, with at
most 32 entries and a 2 MiB limit per response. The key includes the exact
query, tab, and page. Simultaneous identical searches share one upstream
request. Empty responses and errors are retried; nothing is written to disk.
This caches raw strict-safe-search results, never rendered pages or account
permissions. Every request still checks query permission and filters results
against the current account policy and page-scan verdicts. Browser responses
remain `Cache-Control: no-store`.

SearXNG gives upstream engines 2 seconds by default and caps its overall
search wait at 2.5 seconds. It returns the engines that answered within the
budget, so an unusually slow engine may contribute no results. The front end
allows 4 seconds for the local request. A fresh search still depends on outside
engines and their rate limits; this is not a local web index.

The search journal records backend, filtering, and rendering durations without
query text. Use `journalctl -u kosher-search` to distinguish a slow engine from
slow local processing.

An isolated Fedora image test with the real backend and account filtering on
2026-10-15 returned repeated Web, Images, and News searches in 22–35 ms.
Fresh searches in the same small sample took 0.65–2.54 seconds. Preparing the
filter took 1.6 seconds at startup. These are development measurements, not a
latency guarantee for other connections or engines.

## What gets filtered

**The query, before anything runs.** Dropping every result is not enough,
because result snippets are written from the pages the engine found, so
an explicit search shows explicit text even when no link survives. Queries
are matched against a blocklist of terms that have no innocent use, and,
when the account has the language filter on, against the bad-language
list.

A query that is plainly asking for help with the problem is let through:
"pornography addiction help", "how to block porn on my phone". The results
are filtered by category either way, and somebody looking for a way out
must not be met by a block page.

**Each result, by address.** Approved-sites mode keeps only approved
hosts. The other filtered modes drop hosts in a blocked category, and
filtered mode also applies the account's page rules and shop department
rules.

**Each result, by content.** The title and snippet are scored by the same
weighted terms and the same rules that
[judge a page by its words](content-filtering.md#judging-a-page-by-its-words),
including the extra evidence a page that talks about the problem needs.

**Results nothing has ever classified.** This is the gap domain lists
leave open, and it is where new material lives. For results on hosts the
category database has never heard of, the front end fetches the first
128 KB of the page, scores it, and remembers the verdict by host for a
week. A result whose host is known to read worse than the account allows
is hidden.

The reading happens after the search is answered, never before it. A
search shows what the cheap checks allow and what the cache already knows;
up to ten unknown hosts are queued and read in the background, and each
verdict applies from the next search on. Waiting for the fetch made every
fresh search as slow as the slowest site in it, and the protection it
bought is there anyway: the title and snippet are scored before anything
is shown, and in filtered mode the proxy reads the page itself when the
link is clicked.

A page that cannot be fetched is never treated as evidence, and a host
that could not be read is left alone for an hour rather than tried again
by every search that lists it.

## Where searching happens

Being the available search engine is not enough; it has to be the one
people use.

- Firefox ships with it as the default engine and the home page.
- The filtering proxy redirects Google, Bing, DuckDuckGo, Yandex, Brave,
  Ecosia and Startpage result pages to it, so a filtered account that types
  a search anywhere lands on the filtered page. Only result pages: maps,
  autocomplete endpoints and everything else are left alone, because
  redirecting those breaks the site instead of filtering it.
- In approved-sites mode the engines are unreachable anyway, and the start
  page lists the sites the account can open.

## Safe search

Forced in three places: the resolver rewrites the safe-search hostnames,
the proxy rewrites the query parameters, and the search service sends the
strictest setting on the one request the engines see. SearXNG's
preferences are locked so a crafted URL or a cookie cannot lower it.

## Images

SearXNG's image proxy is off. With it on, every thumbnail would be served
from loopback and would bypass the filtering proxy entirely. With it off,
thumbnails load from their origin.

So in filtered mode an account gets image results, because each thumbnail
goes through the proxy and is judged like any other picture. In every
other mode an account that filters pictures does not, because no other
mode reads the connection, and a grid of pictures chosen by a search
engine from pages nobody has vetted is not something to hand such an
account unexamined.

## Lists

Two lists do the work: the search blocklist, explicit terms that will not
run at all, and the content terms a page or a result is judged by. Both
ship complete in sixteen languages and are updated and edited like
[every other list](content-filtering.md#the-lists-shipped-updated-edited).
Both are checked against ordinary text in the test suite: chicken breast
recipes, breast cancer information, biology lessons, Sussex, Middlesex and
a page arguing for modest dress all come back clean.

## Checked against a running stack

The unit tests exercise the decision engine with a stubbed back end, which
is not enough for this part of the system. So the image build loads the
shipped SearXNG settings against the SearXNG it just installed and fails
if the two disagree, and `just check-services` starts the real stack
inside the built image: SearXNG as its unit starts it, a real search
through the front end with every result rendered, an explicit query
refused, and an "ask for this page" landing in the spool.
