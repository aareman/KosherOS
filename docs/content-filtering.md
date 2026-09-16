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

So the block page has a button. It records who asked, for what, and why
(the filter's own reason, "category:video", travels with the request, so
the admin sees "blocked by Video and streaming" beside it instead of
guessing which of five settings was responsible). The admin app shows the
queue as a blue banner under the header of whichever page is open — "2
requests waiting for you" — and each request is answered with one button on
its row. A request carries no authority of its own — nothing changes until an
admin says so — which is exactly what lets the asking be frictionless.

**Nobody has to ask first.** Everything the filter blocks, hides or
refuses is written to an activity log (`kosherd/activity.py`; the proxy
and the search service append to their own files under
`/var/lib/kosher-activity`, the same 0730 root:kosher-spool arrangement as
the request spool, and kosherd is the only reader). The admin app shows it
as an Activity page in the sidebar — the filter's diary, newest first,
narrowable to one person from the header bar — and on each person's own
page as "Blocked today". A blocked page
has an Allow button that does exactly what approving a request does
(`AllowUrl`), so a parent who sees the block can fix it before the child
comes to ask. It is a record of the filter, not of the person: what was
allowed through is never written, so it cannot become a browsing history.
Settings changes are in the same diary with the admin who made them, so a
two-parent household can see who changed what. Everything is trimmed to a
week.

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

All of it appears in the admin app as an amber count beside Protection in
the sidebar, with the full list first on the Protection page — and when
nothing is wrong the badge is simply absent and the page says "Running",
because a healthy machine that says nothing is indistinguishable from one
that was never checked. `kosherctl status` prints the same; `kosherctl lists` prints what each list holds
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

### Three layers

    shipped   what came with the image
    portal    a signed update, so a list improves without an OS rebuild
    edits     this family's handful of additions and removals

A filter that can only improve when the operating system is rebuilt is a
filter that goes stale, so the portal can publish a list bundle. It is
signed with the same Ed25519 key as the policy and replay-protected the
same way, because rolling a family back to last year's word list is an
attack rather than a downgrade — there is a test for exactly that.

The bundle is the same for every enrolled device. A word list is
upstream's business, not a family's, and what a family changes stays on
their own machine: the portal has no reason to know it and no business
holding it.

The portal layer *replaces* the shipped copy, and the family's delta is
applied on top of whichever base is in force. So a portal update brings
every new entry and still keeps a parent's edits — tested directly, by
adding a word, removing another, then shipping an update and checking all
three outcomes survive.

Only four filenames are accepted, by name. A portal that could choose the
filename could write something that is not a list into a directory the
filter reads.

A portal that predates all this returns 404, which is treated as "nothing
new" rather than an error: an older portal should keep working, not start
failing every sync with something nobody can act on.

### The category database is a manifest, not a payload

Five million domains is 190 MB, which cannot ride inside a signed JSON
document. So the bundle carries a **manifest** — a version, a URL, a size
and a SHA-256 — and the device fetches the file itself.

The signature covers the hash, which is the whole point: the download
needs no trust of its own. It can come from a CDN, a mirror, plain HTTP,
and a byte out of place is caught on arrival. The hash is computed as the
bytes stream in, so a mismatch costs one wasted download and never a
wasted disk.

Then the file has to prove it is a catalogue before it is installed: it is
opened, queried, and refused if it holds fewer than a hundred thousand
domains. A truncated database that loads anyway is the worst outcome
available, because it looks exactly like a working one. The old catalogue
stays in place through any failure, and there is a test for that.

### Publishing

`just publish-lists https://portal.example` sends this repository's lists
to a portal, so a family on an older image catches up without waiting for
a new one — publishing is "send what the next image would have shipped".

Add `--catalog-url` to publish a category-database manifest alongside
them. The hash is taken from the **local** file rather than from whatever
the URL returns at that moment, which is the mistake this would otherwise
invite: signing a hash of something you have not seen.

The script sends only the four names a device will accept. Anything else
is silently dropped on arrival, and refusing to send it is better than
discovering that later.

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

## YouTube

Restricted Mode is one switch for the whole site, which is useless for a
family that wants shiurim but not entertainment. So there are three
levers, and the important part is *where* they are applied.

**Per category.** YouTube labels every video with one of fifteen
categories, and a parent can turn any of them off — block Entertainment
and Gaming, keep Education. The settings keep a category's id (`10`);
YouTube's player JSON names it (`Music`, and `Howto & Style` where the
table says How-to); the proxy maps the name to the id, however it is
spelled, and reads the microformat's category rather than the first
`"category"` key it meets. (For a while it compared the name with the id
as they were, and no category limit did anything.)

**Shorts.** Not a category — a short can be in any of them — but a kind of
video all the same, and the one no category limit reaches. It is a switch
of its own in the same list, on by default. Off means the
`/shorts` pages are refused, the reel endpoints and the ordinary player
are answered "cannot be played" when the page asking is a short, and the
Shorts shelves and reel links are taken out of the feeds.

**Thumbnails.** At the modesty picture levels YouTube's thumbnails
(`i.ytimg.com`) are treated as image-search thumbnails are: small pictures
of the whole web from a host the family cannot curate, hidden whenever a
person is in them rather than judged one by one at a size the detector
cannot judge reliably.

**Per channel.** An approved-channel list, which when it has anything in
it is the whole allowance: only those channels play.

**Restricted Mode**, sent as a header on every request, as a floor under
both.

### It has to be enforced in the app, not on the page

YouTube is a single-page application. The first load is HTML; after that,
every video is fetched as JSON from `/youtubei/v1/player` and no watch page
is ever parsed again. A filter that reads only the HTML therefore checks
the first video a child opens and nothing they click afterwards — which is
almost all of them.

So the rules are applied to the player API, and the block is returned in
YouTube's own "cannot be played" shape rather than as a block page. That
response is consumed by the player, not read by a person: a 403 spins
forever, and an HTML page where JSON was expected is a broken app.

### The feeds, for approved-channel accounts only

An account limited to a few channels would otherwise get a home page full
of videos that all fail to play, which teaches a child that the computer
is broken rather than that somebody chose this. So the feed responses are
pruned to the channels that will actually play.

Deliberately conservative: an entry is dropped only if it both looks like
a video renderer **and** names a channel, so anything with an unfamiliar
shape survives untouched. The channel is matched by shape — the id, the
handle, or the byline text — not by searching the response for a name,
because a title mentioning an approved channel is not that channel's
video. Nothing is pruned for accounts that are not limited to a channel
list; playback filtering already covers those, and rewriting a feed
nobody needed rewritten is all risk and no benefit.

## Ad blocking, for everyone

Like a Pi-hole, and for the same reason: the machine already forces every
account's DNS through its own two resolvers (the family one that answers
filtered accounts, the plain one that answers unfiltered ones — see
`kosherd/nft.py`), so a domain list rendered into both reaches every
browser and every app on every account. A browser extension covers one
browser for one account and can be switched off; this cannot be stepped
around from inside a session, and Firefox is told (`DNSOverHTTPS` locked
off) not to carry its DNS elsewhere.

What is blocked is the catalogue's `ads` category, which at image build is
UT1's advertising lists plus **StevenBlack's unified hosts** — Pi-hole's own
default list, adware and tracker domains merged from several reputable
sources, about seventy thousand domains, MIT-licensed. `kosherd/dns.py`
`render_adblock` writes them as `address=/domain/` (NXDOMAIN: one line each,
and an ad that does not resolve is an ad the page never waits for) into
`/etc/kosher/dnsmasq.d/adblock.conf` and `/etc/kosher/dnsmasq-open.d/
adblock.conf`. A dnsfilter account that also blocks the `ads` category is
not given the list twice.

Machine-wide, on by default. It is not content filtering, so it applies to
unfiltered accounts too; switching it **off** is the guardian-gated act
(`SetAdBlock`, the switch on the Protection page under "Administration" in
the admin app, `kosherctl adblock off`), because an ad network is also where
immodest imagery arrives uninvited. It sits there, under "Applies to
everyone", rather than on a person's page, because one resolver cannot
answer differently per account — the page says so next to the switch. What it does not do: cosmetic filtering (an empty box
where an ad was), first-party ads served from the site's own domain, and
ads inside YouTube's own player, which the YouTube limits handle.

## Which settings act in which mode

Twice the interesting bug has been a setting that quietly did nothing for
some kind of account. So it is a matrix test rather than a habit of
checking: for every mode, a user is given every setting a non-default
value, the real enforcement artefacts are rendered, and each setting must
either turn up in one of them or be named as deliberately inert *with the
reason*. A new setting that reaches nothing fails the suite.

Two things fell out of writing it.

**The search service was given settings it could not act on.** Its policy
file carried rules, whitelists and categories for unfiltered accounts —
harmless in itself, and exactly the sort of file somebody later reads as
evidence that those settings are applied. It now carries only what the
account's mode can use; an unfiltered or no-internet account is one word.

**Two controls were greyed out although they still act.** "Pictures and
video" and "Bad language" were disabled outside filtered mode, with
subtitles saying pictures can only be filtered there. That stopped being
true: the media level also decides whether image search results are shown
at all, and the language setting also blocks searches containing bad
language. Both are editable again in every mode that enforces anything,
with subtitles that say exactly which half applies. Disabling a control
that still acts is worse than a wordy subtitle.

YouTube stays greyed out everywhere but filtered mode, and that one is
genuine: nothing outside the proxy can see which video is playing.


## Ready-made approved-site lists

"Whitelist only" is the strongest thing this product offers and the most
work to set up: Sefaria alone loads from half a dozen hosts, and a page
whose fonts and scripts are blocked looks *broken* rather than blocked —
which is how a filter ends up switched off. So the image ships bundles
(`os-image/files/usr/share/kosher/whitelist-bundles.json`,
`kosherd/whitelists.py`) that a parent switches on instead of typing
domains:

- **Torah study** — Sefaria, YUTorah, TorahAnytime, the Daf Yomi sites,
  Chabad.org, HebrewBooks, OU Torah, Alhatorah, Mechon Mamre and the rest,
  with the audio and asset hosts they use.
- **Email and files** — Gmail, Outlook, OneDrive, Google Drive, Dropbox,
  Proton, iCloud, with the sign-in and content hosts each needs.
  Deliberately *not* `google.com` or `microsoft.com` as a whole: entries
  include their subdomains, so a bare `google.com` would open search and
  YouTube alongside Gmail. There is a test asserting those stay out.

Any number can be on at once, and a set of shared hosts (fonts, script
CDNs) rides along whenever any bundle is on. The account's own list is
merged with them, so a family still adds its shul's website by hand. The
merge happens in `whitelists.effective()` and is what `dns.py` renders,
so switching a bundle on reaches the resolver on the next apply.

`kosherctl site-lists show` prints the bundles;
`kosherctl site-lists set <user> torah` switches them on without the GUI.

And in the admin app, a whitelist account is **not** asked which kinds of
site to block: it reaches its list and nothing else, so the category grid
would be fifteen toggles that change nothing. The approved lists take
their place on that tab.

## Developer tools on a filtered machine

In "Filtered internet" mode the machine reads the account's HTTPS with its
own certificate authority. Browsers, curl, git and Go trust it through the
system store, which `mitmca.install` adds the anchor to. The language
package managers do not: npm, yarn, pnpm and bun (Node's own bundle), pip
and requests (certifi), uv, gem, cargo and deno each carry their own
certificates and would refuse every download with a certificate error,
which to a student looks like "the internet is broken".

So the image sets, for every account, the environment each tool honours:
`SSL_CERT_FILE`, `REQUESTS_CA_BUNDLE`, `PIP_CERT`, `NODE_EXTRA_CA_CERTS`,
`CARGO_HTTP_CAINFO` and `GIT_SSL_CAINFO` at the system bundle
(`/etc/pki/tls/certs/ca-bundle.crt`, which contains the KosherOS authority
once inspection is set up and is Fedora's ordinary bundle otherwise), and
`DENO_TLS_CA_STORE=system`, `UV_NATIVE_TLS=1`. Twice: `/etc/profile.d/kosher-ca.sh`
for login shells and `/etc/environment.d/50-kosher-ca.conf` for programs
started from the desktop. Java is deliberately left out: `JAVA_TOOL_OPTIONS`
prints a line on every JVM start; Fedora's trust store already extracts a
Java keystore at `/etc/pki/ca-trust/extracted/java/cacerts` for anyone who
needs it.

And the registries those tools fetch from — npm, yarn, bun, PyPI, astral,
RubyGems, crates.io, deno.land, JSR, the Go proxy, GitHub release assets —
are in `DEVELOPER_REGISTRIES` (`policy.py`), reachable from every account
like the OS's own update and Flathub hosts. They hold code, not pages, and
a school or work project on this computer needs them on day one.

## Who is connecting: one port per user

The proxy has to know which person a connection belongs to, because two
people at the same machine get different filtering. nftables already knows
— it dispatches every packet by the owning uid (`meta skuid`) — so rather
than redirect everyone to one proxy port and have the proxy rediscover the
owner afterwards, kosherd gives **each filtered user their own loopback
port** (30000, 30001, … in uid order) and redirects them to it. The proxy
listens on exactly that set and reads the user from the port a connection
arrived on. One map lookup, deterministic, nothing to race.

The first design redirected everyone to port 8080 and scanned
`/proc/net/tcp` for the client's source port. A few `curl`s never caught
what a browser's connection churn did: a just-closed connection lingers in
TIME_WAIT reporting uid 0, Linux reuses its port for a new connection to a
different server, and the scan found the ghost first — so a child became
"root", a uid with no policy, which then meant the open web. The
`kosherd.uidmap` lookup still exists (the search service uses it, matching
the full 4-tuple on live sockets only), and the proxy falls back to it
only for a rules file that predates per-user ports.

"Who is at the keyboard" would not have been enough either: GNOME's Switch
User leaves the previous session running, and a browser left open there
keeps making requests under its own uid while somebody else is active.
The owner of the socket is the only right answer, and the port carries it.

Whatever the identification, a uid the proxy holds no policy for gets the
fail-closed floor (the default categories, immodest media, substituted
language), never the open web. Every connection reaching the proxy is a
filtered user's by construction, so an unknown one is a fault to log —
and it is logged — not a stranger to wave through.
