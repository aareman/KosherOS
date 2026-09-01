# Search

## Why the OS ships a search engine

A filter that only blocks pages makes searching miserable. Every search
returns ten links, some number of which lead to a block page, and the
person has to guess which. In whitelist mode it is worse than miserable:
the whitelist is invisible, so there is no way to find out what the
computer will open short of typing addresses and seeing what happens.

So KosherOS filters the *results* with the same policy that filters the
traffic. A filtered user never sees a link into a blocked category. A
whitelist user searching for "kosher recipes" gets back the whitelisted
sites that match — the whitelist becomes browsable, which is the first
time it is discoverable at all.

## The pieces

    browser ──▶ kosher-search (127.0.0.1:8888)  ──▶ SearXNG (127.0.0.1:8889) ──▶ engines
                  │  who is asking (uidmap)
                  │  may this query run?      (search.query_reason)
                  │  may this result be shown? (search.ResultFilter)
                  └─ read the page if nothing knows it (pagescan)

`kosher-search` is a small stdlib HTTP server. It accepts the connection
itself, which is the only reliable way to learn the client's source port
and therefore, via `/proc/net/tcp`, which user is searching. Filtering is
per user, so that identity is the whole point.

SearXNG sits behind it and does the one job that genuinely needs a large
dependency: scraping a dozen search engines that keep changing. It is
reached only over its JSON API, which is stable, so a SearXNG upgrade
cannot reach our filtering. Its own plugin API — which does churn between
releases — is not used at all.

The firewall rejects any human user's connection to port 8889. Loopback
is otherwise wide open (that is what lets a user reach the search page),
and the engine's raw results carry exactly the snippets the filter exists
to withhold.

## What gets filtered

**The query, before anything runs.** Dropping every result is not enough:
result snippets are written from the pages the engine found, so an
explicit search shows explicit text even when no link survives. Queries
are matched against a 119-term blocklist of terms that have no innocent
use, and — when the account has the language filter on — against the
profanity list.

A query that is plainly *asking for help* with the problem is let through:
"pornography addiction help", "how to block porn on my phone". The
results are filtered by category either way, so nothing is exposed, and
somebody looking for a way out must not be met by a block page.

**Each result, by address.** Whitelist mode keeps only whitelisted hosts.
Other filtered modes drop hosts in a blocked category, and filtered mode
also applies the account's URL rules.

**Each result, by content.** `kosherd/content.py` scores the title and
snippet against weighted terms. Terms sit at the strongest verdict they
can support — `nsfw`, `suggestive`, `immodest` — and points earned at one
level count toward every milder level. So explicit words also make a page
immodest, but no pile of swimwear vocabulary can ever add up to explicit.
An earlier version used a single flat score and ranked a bikini catalogue
with pornography.

Pages that talk *about* the problem — an addiction helpline, a review of
filtering software, a shiur on shmiras einayim — need twice the evidence,
because blocking those works against the family that installed this.

**Results nothing has ever classified.** This is the gap domain lists
leave open, and it is where new material lives. For results on hosts the
category database has never heard of, `pagescan.py` fetches the first
128 KB, scores it, and hides the result if the page reads worse than the
account allows. Capped at ten scans per search, run in parallel with a
2.5 s timeout, and cached by host for a week. A page that cannot be
fetched is never treated as evidence: dropping every slow or offline site
would make search useless on a bad connection.

## Where searching happens

Being the *available* search engine is not enough; it has to be the one
people actually use.

* Firefox ships with it as the default engine and the home page.
* The filtering proxy redirects Google, Bing, DuckDuckGo, Yandex, Brave,
  Ecosia and Startpage result pages to it, so a filtered user who types a
  search anywhere lands on the filtered page. Only result pages —
  `/maps`, autocomplete endpoints and everything else are left alone,
  because redirecting those breaks the site instead of filtering it.
* In whitelist mode the engines are unreachable anyway, and the start
  page lists the sites the account can open.

## Safe search

Forced in three places, deliberately: the resolver rewrites the safe
search hostnames, the proxy rewrites the query parameters, and the search
service sends `safesearch=2` on the one request the engines actually see.
SearXNG's preferences are locked so a crafted URL or a cookie cannot lower
it.

## Images

`image_proxy` is off. With it on, every thumbnail would be served from
127.0.0.1 and would therefore bypass the filtering proxy entirely — image
results would be the one part of the web nothing inspected. With it off,
thumbnails load from their origin, so a whitelist user simply does not get
them and a filtered user gets them through the proxy like any other
picture.

Beyond that, whether an account gets image results depends on whether
anything will actually look at the thumbnails.

In **filtered** mode they do. `image_proxy` is off, so a thumbnail loads
from its origin, through the proxy, and is judged exactly like any other
picture — withholding image search from those accounts would be refusing
something the filter is already handling.

In every other mode they do not, because no other mode reads the
connection. A grid of pictures chosen by a search engine from pages nobody
has vetted is not something to hand an account that asked for pictures to
be filtered, and there is nowhere else to check them.

## Lists

Both lists ship complete and are meant to stay that way. An administrator
may add or remove a handful of terms; nobody should have to build one.

* `/usr/share/kosher/search-blocklist.json` — 119 explicit search terms.
* `/usr/share/kosher/content-terms.json` — 173 weighted content terms.

An admin- or portal-supplied copy under `/var/lib/kosher/` overrides the
shipped one. Both are checked against ordinary text in the test suite:
chicken breast recipes, breast cancer information, biology lessons,
Sussex, Middlesex and a page arguing *for* modest dress all come back
clean.

## Verified against a running stack

The unit tests exercise the decision engine with a stubbed back end. That
is not enough for this part of the system, and three faults proved it —
each one shipped, and none of them was reachable from a unit test:

* **SearXNG was not installed at all.** `pip install` of its repository
  fails outright: its `setup.py` imports `searx/__init__.py`, which
  imports `msgspec` before anything is installed. The build swallowed that
  into a warning. It is now cloned and its `requirements.txt` installed,
  which is upstream's own instruction, and the unit runs it from the tree.
* **Our `settings.yml` was invalid.** `preferences.lock` carried
  `safe_search`, which is not a lockable preference (the name is
  `safesearch`, already on the list). SearXNG validates this and refuses
  to start — correctly, and it is how this was found.
* **Three "disabled" engines did not exist.** Naming an engine SearXNG
  does not ship does not disable anything; it *defines* a new one with no
  `engine:` field, which fails to load and logs an error at every startup.

The last two are now build-time checks: the image build loads our
`settings.yml` against the SearXNG it just installed and fails if either
disagrees, so a bad setting cannot ship again.

The whole stack is exercised against a live back end — SearXNG started
exactly as its unit starts it, a real search through the front end, 27
results in and 27 rendered, an explicit query still refused, and an
"ask for this page" landing in the spool.

One log line remains and is upstream's: SearXNG loads the `torch` onion
engine's module before it checks whether the engine is disabled, so it
logs a registration failure at startup for an engine that has no Tor proxy
to reach and that we disable anyway.
