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
