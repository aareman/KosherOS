# Content filtering: getting to MBSmart / Gentech level

## Where we are

KosherOS today filters by **identity of the destination**, not by what is
on the page:

- Cloudflare family DNS (1.1.1.3) blocks known adult and malware domains;
- whitelist mode allows only named domains;
- filtered mode decrypts HTTPS locally and applies **admin-written URL
  rules** (`youtube.com/shorts*`).

That is a real filter, but it is not what a family expects from a kosher
filter. Cloudflare's list is broad and safety-oriented, not modesty-
oriented: it misses immodest imagery on mainstream sites, most social
media, video platforms, shopping sites with suggestive advertising, and
anything new. And an admin cannot realistically hand-write rules to cover
the web.

The gap between "blocks porn domains" and "a frum family trusts this
machine" is the whole product.

## What the incumbents actually do

Worth being clear-eyed, because it sets the bar and the cost:

- **A curated category database.** Millions of domains and URLs, classified
  (adult, dating, social, video, shopping, news, gambling…). This is the
  core asset. It is maintained continuously by paid staff and is why these
  services are subscriptions rather than products.
- **Human review.** A queue where users request access to a site and a
  reviewer decides — usually same-day. This is what makes a strict filter
  livable, and it is a staffing commitment, not a feature.
- **Per-customer profiles.** A rav or the vendor sets levels; families pick
  one and request exceptions against it.
- **Image and video handling.** Blocking or blurring images on otherwise
  allowed pages, and turning off video/thumbnails on search and shopping.
- **Search enforcement.** Forcing SafeSearch on Google/Bing/YouTube, often
  restricting YouTube to approved channels.
- **Reporting.** What was visited, what was blocked, sometimes to a third
  party (an accountability partner).

## What KosherOS should build, in order

The interception layer already exists — every filtered user's HTTPS passes
through our proxy, where the full URL and the response body are visible.
Everything below is a policy layer on top of infrastructure we have.

### 1. Category lists (the foundation) — BUILT

The image now imports the University of Toulouse blacklists at build time:
**10.5 million domains across 24 categories** (4.6M of them adult), stored
as SQLite at 190 MB, answering a lookup in **4.6 microseconds** with about
60 MB resident. A Python dict of the same data would cost hundreds of
megabytes of RAM, which the family machine does not have.

Spot-checked against sites that matter: chinuch.org, torahanytime.com,
artscroll.com, ou.org, chabad.org, sefaria.org, hebrewbooks.org, yeshiva.co,
matzav.com, theyeshivaworld.com, kosher.com, wikipedia.org and several
government and medical sites all come back clean, while pornhub, xvideos,
onlyfans, chaturbate, stripchat, tinder, bet365, nordvpn and hidemyass are
all caught. A list that blocked chinuch.org would be worse than no list, so
that check runs before shipping.

### The original plan for this piece

A signed, versioned list bundle the device downloads and the proxy consults
per request: `{domain or URL pattern -> categories}`. Per-user policy then
says which categories are allowed.

- Start from open sources — Shalla, UT1 (Toulouse), StevenBlack, the Hagezi
  lists — which cover adult/gambling/social reasonably and are free.
- Ship it as data over the **portal**, so lists update without an OS
  release; sign it with the same Ed25519 key as policy.
- Design the format for a curated list of our own to replace or extend the
  open ones later. That curation is the long-term moat and the long-term
  cost.

### 2. Enforced safe search and video restriction

Cheap, high value, entirely mechanical in the proxy:

- rewrite Google/Bing/DuckDuckGo queries to force SafeSearch;
- send YouTube's `Restricted Mode` header, or map it to Restrict Moderate
  via DNS CNAME (`restrict.youtube.com`);
- optionally allow YouTube only for an approved channel list.

### 3. Request-and-approve

The block page becomes useful instead of a dead end: "ask for this page".
The request goes to the portal, an admin (or a reviewer at a service) sees
the URL and who asked, and approves or denies. Approvals become URL rules
automatically.

This is what makes strict settings tolerable, and we already have the two
halves — a block page and a portal that pushes signed policy.

### 4. Image handling

The proxy can already see response bodies. Options in increasing cost:
strip images by MIME type on flagged domains; blur them client-side with an
injected stylesheet; or run a local nudity classifier (NudeNet-class model,
CPU-only) on images above a size threshold. The last is the only one that
generalises to unknown sites, and is the most expensive per request — worth
prototyping only after 1–3.

### 5. Reporting

A per-user log of allowed and blocked URLs, visible to the admin and
optionally mailed to an accountability partner. Cheap to build once the
proxy records decisions; needs a clear retention policy, since it is the
most privacy-sensitive thing in the product.

## The honest constraints

- **Interception is required** for anything above domain level, and it
  means the machine decrypts that user's HTTPS. Already true of filtered
  mode, and it must stay visible in the UI.
- **A curated list is staff, not code.** We can ship open lists on day one,
  but matching MBSmart means someone maintaining categories and answering
  requests. That is a business decision.
- **Apps are not the browser.** A phone-style app can bypass URL rules with
  certificate pinning; the app allowlist is what handles that, and the two
  layers have to be designed together.
- **Over-blocking is the usual failure.** Every layer needs the request
  path from (3), or families disable the filter entirely.

## Suggested first slice

Category lists (1) plus safe search (2), with the list delivered by the
portal. That is a visible jump in filtering quality, uses infrastructure
that already exists, needs no new staff, and makes the "filtered internet"
mode behave the way someone expects when they read the label.

## Asking for a page

Every filter is wrong sometimes. What decides whether a family keeps using
one is not how often it is wrong, it is what happens next. If the answer is
"find the parent, get them to open a settings app, work out which of five
settings caused it and type the address in by hand", the real answer is
that the filter gets turned off.

So the block page has a button. It records who asked, for what, and why;
the admin app puts the queue at the top of the page with one button to
allow it. A request carries no authority of its own — nothing changes
until an admin says so — which is exactly what lets the asking be
frictionless.

**Where the form posts.** To the blocked site's own origin, on a reserved
path (`/__kosheros__/request`), which the proxy answers itself and never
forwards. A local `http://` address would be mixed content from a page the
browser considers `https`, and browsers refuse to submit that.

**The spool.** Requests are individual files in `/var/lib/kosher-requests`,
mode 0730 root:kosher-spool. The two unprivileged services that can create
one (the proxy and the search service) are in that group: they may drop a
request, they may not list the directory or read anyone else's. kosherd,
running as root, is the only reader. Ids are validated as 32 hex characters
before they are turned into paths.

**What approving does depends on the account's mode**, because making the
admin work that out is the same friction again. A whitelist account gets
the domain on its whitelist — an allow rule would do nothing there, since
its traffic never reaches the proxy. A filtered account gets an allow rule
placed *ahead* of the existing rules, because one added after whatever
blocked the page would never be reached. The admin chooses "just this page"
or "all of this site"; a whitelist account is only offered the site, since
that is all its enforcement can express.

## Saying when the filter is not filtering

Every list loader here fails open and quiet. Miss the category database and
the bundle is empty; miss the content terms and the scorer matches nothing;
miss the word list and the profanity filter is off. That behaviour is right
— a missing file must not take the machine down — and the silence is the
part that is wrong, because the account still says "Filtered internet"
while nothing it covers is filtered.

`kosherd/selfcheck.py` counts what actually loaded and reports anything
empty or truncated in words a person can act on. A truncated list is the
worse case of the two, because it looks like it is working.

`FilterStatus` gathers that with three other facts about the machine right
now, as opposed to what is configured:

* which services should be running and are not — only the ones this
  household's modes actually need, because a warning that does not matter
  teaches people to ignore warnings;
* whether pictures are being checked, or hidden because the machine cannot
  keep up, or hidden because no model is installed;
* whether the inspection certificate is generated, in the trust store, and
  the same in both places.

That last one is worth the space. Without a trusted CA, every HTTPS page a
filtered account opens shows a certificate warning — the person sees a
broken internet and a scary security error, and nothing connects that to
the filter. A stale anchor (the CA regenerated without refreshing the
trusted copy) gives the identical symptom and is harder to spot, so it is
checked by comparing the two files rather than by their existence.

All of it appears at the top of the admin app under "Needs your attention",
and in `kosherctl status`; `kosherctl lists` prints what each list holds
and where it came from.

## Where an admin override goes

Lists ship complete and can be replaced or extended by an admin or the
portal. That override used to be written under `/var/lib/kosher`, which is
mode 0700 because it holds the policy — so the filtering proxy and the
search service, both unprivileged, could not read a single one of them.
They fell back to the shipped copy and said nothing, which meant the
documented behaviour simply did not happen.

Overrides live in `/var/lib/kosher-lists` instead: a directory of nothing
but lists, world readable, with no secrets in it. There is a test
asserting no list is ever looked for under the private state directory
again, and `just check-services` proves the proxy — running as its own
unprivileged user — actually picks an override up.

### An edit is a delta, not a copy

The lists ship complete on purpose. What a family will actually do is
disagree with a handful of entries: a word they want cleaned that is not
listed, a term that keeps blocking something innocent.

If that edit were a copy of the whole list, the family would silently stop
receiving every later improvement to it, and nobody would notice for a
year. So an override is `{"add": ..., "remove": [...]}`, applied on top of
whatever ships — add one word today and next year's shipped additions
still arrive. A complete replacement is still honoured, because the portal
may legitimately want to send one.

Three lists are editable this way: the bad-language list, the blocked
search terms, and the words pages are judged by. The shop rules and the
five-million-row category database are not, because neither is a thing to
hand somebody a text box for.

`EditList` caps an edit at a hundred entries, and the refusal says why:
the answer to needing more is not "try again with fewer", it is that the
shipped list is not finished and should be fixed for everybody. `GetListEdits`
reports how many entries ship alongside how many the family has changed,
because "119 ship with KosherOS, you have added two" is the sentence that
stops somebody trying to build the list themselves.

From the command line: `kosherctl words show words`,
`kosherctl words add words blast --replacement bother`,
`kosherctl words remove searches "bikini atoll"`.
