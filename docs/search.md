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
filter has read what it needed to.

SearXNG sits behind it and does the one job that needs a large dependency:
scraping a dozen search engines that keep changing. It is reached only
over its JSON API, which is stable, so a SearXNG upgrade cannot reach the
filtering. Its plugin API, which changes between releases, is not used.

The firewall refuses any human account's connection to SearXNG's own port.
Loopback is otherwise open, which is what lets a person reach the search
page, and the engine's raw results carry exactly the snippets the filter
exists to withhold.

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
weighted terms that judge a page. Terms sit at the strongest verdict they
can support, and points earned at one level count toward every milder
level, so explicit words also make a result immodest but no pile of
swimwear vocabulary can add up to explicit. Pages that talk about the
problem need twice the evidence.

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

Both lists ship complete and are meant to stay that way. An administrator
may add or remove a handful of terms; nobody should have to build one.

- the search blocklist, explicit terms that will not run at all;
- the content terms, the weighted words a page or a result is judged by.

Both ship in sixteen languages, and a family's edits or a portal's update
override the shipped copy. Both are checked against ordinary text in the
test suite: chicken breast recipes, breast cancer information, biology
lessons, Sussex, Middlesex and a page arguing for modest dress all come
back clean.

## Checked against a running stack

The unit tests exercise the decision engine with a stubbed back end, which
is not enough for this part of the system. So the image build loads the
shipped SearXNG settings against the SearXNG it just installed and fails
if the two disagree, and `just check-services` starts the real stack
inside the built image: SearXNG as its unit starts it, a real search
through the front end with every result rendered, an explicit query
refused, and an "ask for this page" landing in the spool.
