# Content filtering

A kosher filter has to do more than block known bad sites. It has to read
the page, because the immodest picture is on an allowed news site and the
bad language is on a page that is otherwise fine. This page describes what
the filter looks at, in the order it looks, and what a parent can change.

Pictures and video have [a page of their own](media-filtering.md), and so
does [search](search.md).

## The layers

Every account's DNS goes through the machine's own resolver, which answers
from a family-filtered upstream and refuses ad and tracker domains. That is
the floor for everyone.

For an account in *Filtered internet* mode the machine also reads the
account's HTTPS, with its own certificate authority, so the full address and
the page itself are visible. That is where everything below happens, in
this order, and each step runs only because the one before it could not
settle the question:

| Step | Decides | Cost |
|---|---|---|
| the category of the site | most of the bad web | microseconds |
| the account's page rules | a path a parent allowed or blocked by hand | microseconds |
| the shop's department, from the address | the lingerie department of a shop the family uses | microseconds |
| the words on the page | a page nobody has catalogued | milliseconds |
| the bad-language list | words replaced with a milder one | milliseconds |
| the pictures and video on the page | what is hidden or covered | see [pictures and video](media-filtering.md) |

The other modes do less. *Basic protection* has the resolver and nothing
more. *Approved sites only* reaches a list of sites and nothing else.
*No internet* reaches nothing.

## Which ports an account may use

A filtered account's network is default-deny. Every TCP connection it opens,
on any port, is redirected into that account's own proxy listener, so a web
page served on 8080 meets the same rules as one on 443. Loopback is never
redirected. Anything that reaches the proxy and is not HTTP, or TLS carrying
HTTP, is refused rather than passed through.

Three things do not go through the proxy, because they are not web and the
proxy could not read them:

- **Mail.** IMAPS, SMTPS and submission are allowed directly.
- **Extra ports** an administrator grants one account, under *Beyond the
  web* on the account's page, for a program that is neither web nor mail.
  The filter's own ports can never be granted this way. Empty by default.
- **Video calls.** Zoom, Meet and school calls send picture and sound over
  UDP to high ports, which cannot be inspected. That is allowed by default
  so calls work without a visit to the admin app. The switch on the
  account's page says that this is the one channel the filter cannot read.
  Below port 1024 an account gets only NTP, and QUIC stays refused.

A *Basic protection* account has no proxy, so for it the web is ports 80
and 443 to anywhere, plus the same named protocols. *Approved sites only*
and *No internet* allow nothing beyond the approved sites.

## The category database

The image imports the University of Toulouse blacklists at build time:
about ten million domains across twenty-four categories, stored as SQLite.
The database is around 190 MB on disk, answers a lookup in a few
microseconds and keeps about 60 MB resident. The same data in a Python
dictionary would cost hundreds of megabytes of RAM, which the family
machine does not have.

A parent chooses which categories an account may not reach. The strict
default blocks adult, gambling, dating, social, video and more; the first
administrator's own account blocks adult, gambling, dating and filter
bypasses.

Before a build ships, the list is checked against sites that matter.
chinuch.org, torahanytime.com, artscroll.com, ou.org, chabad.org,
sefaria.org, hebrewbooks.org and several government and medical sites must
come back clean, while the adult, dating, gambling and VPN sites in the
check must be caught. A list that blocked chinuch.org would be worse than no
list.

## Page rules

A page rule is the one thing a parent writes by hand, and most families
never need to. Rules are an ordered allow and block list, first match wins,
matched without regard to case, scheme or a leading `www`. A bare host
covers the whole site, `*.host` includes subdomains, and `host/dir/*` covers
a directory. An unmatched request is allowed, so a trailing `block *` makes
an account deny-by-default.

Rules are edited on the person's page in the admin app, or with
`kosherctl rules`.

## Judging a page by its words

A page on a site nobody has catalogued is scored against a list of weighted
terms. Terms sit at the strongest verdict they can support, `nsfw`,
`suggestive` or `immodest`, and points earned at one level count toward
every milder level. Explicit words therefore also make a page immodest, but
no amount of swimwear vocabulary can add up to explicit.

Pages that talk about the problem need twice the evidence: an addiction
helpline, a review of filtering software, a shiur on shmiras einayim.
Blocking those works against the family that installed the filter.

The same scorer judges search results, and the block page's reason names
the verdict so a parent can see why.

## Bad language

Words on the bad-language list are replaced on the page with a milder word,
so a page that is otherwise fine stays readable. The same list blocks a
search that contains one of its words.

The setting is per account, and the strict default has it on.

## Other languages

The word lists ship in sixteen languages, chosen for where Jewish families
live: English, Hebrew, Yiddish, Russian, Ukrainian, French, Spanish,
Portuguese, German, Italian, Dutch, Hungarian, Polish, Arabic, Persian and
Turkish. Each is one file under `os-image/lists/`, and the three shipped
files are built from them with `just lists`. A test fails if they drift.

The matchers understand what those lists need. A word is a whole word in
any script, and in Hebrew and Arabic also behind the prefixes those
languages attach, so והביקיני counts as ביקיני while מזונות is left alone.
Vowel points and accents are ignored. An accented letter matches its plain
spelling where the accent only marks stress, so cabrón matches cabron, but
ñ is not n and ö is not o. The bad-language list compiles one pattern per
script and runs only the ones a page contains.

Two limits are known. Nothing tells the proxy what language a page is in, so
a word two Latin-script languages spell alike gets one replacement. And the
scorer's plural rule is English, so the other lists carry their own forms.

A weekly sweep runs the shipped lists over real pages in every language, as
the proxy would, and fails when ordinary pages read as explicit too often,
a language's pages are rewritten wholesale, or a sample of the adult
category slips through. `just sweep` runs it locally.

## Shopping sites

A shop is not a site a family blocks. A department on it is. Shop rules
match the department from the address before a byte of the page is
fetched, remove the department's entries from the shop's own navigation,
and filter the shop's search box and its autocomplete. They apply to
accounts that block the immodest category. [Pictures and
video](media-filtering.md#shopping-sites) describes how they work.

## YouTube

Restricted Mode is one switch for the whole site, which is useless for a
family that wants shiurim but not entertainment. So an account has four
levers, and all of them are applied inside the app rather than on top of
it.

- **Per category.** YouTube labels every video with one of fifteen
  categories, and a parent can turn any of them off.
- **Shorts.** A short can be in any category, so it is a switch of its own,
  on by default. Off means the Shorts pages are refused, the player answers
  "cannot be played" for a short, and the Shorts shelves are taken out of
  the feeds.
- **Per channel.** An approved-channel list which, when it has anything in
  it, is the whole allowance.
- **Restricted Mode**, sent as a header on every request, as a floor under
  the rest.

YouTube is a single-page application: the first load is HTML, and after
that every video is fetched as JSON from the player API, on the site's own
hosts and on `youtubei.googleapis.com`. The rules are applied to that API,
and a blocked video is answered in YouTube's own "cannot be played" shape
rather than with a block page, because the player consumes the answer and
a page where it expected JSON is a broken app.

A video opened by its address is a page and gets the KosherOS block page
with its "ask for this page" form. A video clicked inside the app gets
YouTube's error frame. The words are the same in both: "Blocked by
KosherOS", the reason, and that the administrator of this computer can be
asked for it. A third message is YouTube's own, when Restricted Mode blocks
a video by itself.

Hovering a thumbnail plays the video, so for any account with YouTube
limits the inline player is taken out of the feed and the animated preview
is hidden. For an account limited to a few channels, the feeds are pruned
to the channels that will play, so a child is not shown a home page of
videos that all fail. The pruning is conservative: an entry is dropped only
if it looks like a video and names a channel.

At the modesty picture levels YouTube's thumbnails are treated like image
search thumbnails: hidden whenever a person is in them, because they are
small pictures of the whole web from a host the family cannot curate.

## Ad blocking, for everyone

The machine already forces every account's DNS through its own resolvers,
so a domain list rendered into them reaches every browser and every app on
every account, the way a Pi-hole does it. A browser extension covers one
browser for one account and can be switched off; this cannot be stepped
around from inside a session, and Firefox is told not to carry its DNS
elsewhere.

What is blocked is the category database's `ads` category: the Toulouse
advertising lists plus fourteen ad and tracker lists fetched individually
from their maintainers, about sixty-five thousand domains in all. [Third-party
notices](third-party.md) lists each one. They are written into the
resolver as non-existent domains, so an ad that does not resolve is an ad
the page never waits for.

It is on by default and applies to unfiltered accounts too, because it is
not content filtering. Switching it off takes the guardian password, since
an ad network is also where immodest imagery arrives uninvited. The switch
sits on the Protection page under "Applies to everyone", because one
resolver cannot answer differently per account. It does not do cosmetic
filtering, first-party ads served from the site's own domain, or ads inside
YouTube's player, which the YouTube limits handle.

## Asking for a page, and the activity log

Every filter is wrong sometimes. What decides whether a family keeps using
one is what happens next.

So the block page has a button. It records who asked, for what, and the
filter's own reason, so the admin sees "blocked by Video and streaming"
beside the request instead of guessing which of five settings was
responsible. The admin app shows waiting requests as a blue banner under
the header of whichever page is open, and each request is answered with
one button: just this page, the whole site, or no. A request carries no
authority of its own. Nothing changes until an admin says so.

What approving does depends on the account's mode. An approved-sites
account gets the site on its list, because its traffic never reaches the
proxy and a page rule would do nothing. A filtered account gets an allow
rule placed ahead of the existing rules, because one added after whatever
blocked the page would never be reached.

Nobody has to ask first. Everything the filter blocks, hides or refuses is
written to an activity log, which the admin app shows as the Activity page,
newest first, narrowable to one person, and on each person's own page as
"Blocked today". A blocked page there has an Allow button that does what
approving a request does, so a parent who sees the block can fix it before
the child comes to ask.

It is a record of the filter, not of the person. What was allowed through
is never written, so it cannot become a browsing history. Settings changes
appear in the same log with the admin who made them, so a two-parent
household can see who changed what. Everything is trimmed to a week.

The request form posts to the blocked site's own origin on a reserved path
the proxy answers itself and never forwards, because a browser refuses to
submit a plain-HTTP form from a page it considers secure. Requests are
individual files in a spool that the two unprivileged services may write
to but not list or read, and the daemon is the only reader.

## Saying when the filter is not filtering

Every list loader fails open and quiet, which is right: a missing file must
not take the machine down. The silence is the dangerous part, because the
account still says "Filtered internet" while nothing it covers is filtered.

So the daemon counts what loaded and reports anything empty or truncated,
together with three other facts about the machine right now:

- which services should be running and are not, counting only the ones
  this household's modes need, because a warning that does not matter
  teaches people to ignore warnings;
- whether pictures are being checked, or hidden because the machine cannot
  keep up, or hidden because no model is installed;
- whether the inspection certificate is generated, in the trust store, and
  the same in both places. A stale copy gives every HTTPS page a security
  warning, and nothing connects that to the filter unless this check does.

All of it appears as an amber count beside Protection in the admin app's
sidebar, with the full list first on the Protection page. When nothing is
wrong the badge is absent and the page says "Running", with the time it
was checked. `kosherctl status` prints the same, and `kosherctl lists`
prints what each list holds and where it came from.

## The lists: shipped, updated, edited

Every list ships complete, and a family should never have to build one.
Three layers sit on top of each other:

| Layer | What it is |
|---|---|
| shipped | what came with the image |
| portal | a signed update, so a list improves without an OS rebuild |
| edits | this family's handful of additions and removals |

Overrides live in `/var/lib/kosher-lists`, a world-readable directory of
nothing but lists, because the filtering proxy and the search service run
unprivileged and must be able to read them.

**The portal layer** is a bundle signed with the same key as the policy and
protected against replay the same way, because rolling a family back to
last year's word list is an attack rather than a downgrade. The bundle is
the same for every enrolled device, and it replaces the shipped copy. Only
four file names are accepted. A portal that does not offer lists is treated
as "nothing new". `just publish-lists https://portal.example` sends this
repository's lists to a portal.

**The category database** is too large to ride inside a signed document,
so the bundle carries a manifest with a version, a URL, a size and a
SHA-256, and the device fetches the file itself. The signature covers the
hash, so the download can come from anywhere, plain HTTP included, and a
byte out of place is caught on arrival. Before it is installed the file is
opened, queried, and refused if it holds fewer than a hundred thousand
domains. The old database stays in place through any failure.

**A family's edit is a delta, not a copy.** An override is a set of
additions and removals applied on top of whatever ships, so adding a word
today does not stop next year's shipped additions from arriving. Three
lists can be edited this way: the bad-language list, the blocked search
terms, and the words pages are judged by. An edit is capped at a hundred
entries, and the refusal says why: the answer to needing more is that the
shipped list should be fixed for everybody. The admin app shows how many
entries ship beside how many the family has changed. From the command line,
`kosherctl words` shows, adds and removes entries.

## Ready-made approved-site lists

"Approved sites only" is the strongest mode and the most work to set up by
hand. Sefaria alone loads from half a dozen hosts, and a page whose fonts
and scripts are blocked looks broken rather than blocked. So the image
ships bundles a parent switches on instead of typing domains:

- **Torah study**: Sefaria, YUTorah, TorahAnytime, the Daf Yomi sites,
  Chabad.org, HebrewBooks, OU Torah, Alhatorah, Mechon Mamre and the rest,
  with the audio and asset hosts they use.
- **Email and files**: Gmail, Outlook, OneDrive, Google Drive, Dropbox,
  Proton and iCloud, with the sign-in and content hosts each needs, and
  not `google.com` or `microsoft.com` as a whole, because that would open
  search and YouTube alongside Gmail.

Any number can be on at once, a set of shared hosts for fonts and script
CDNs rides along whenever any bundle is on, and the family's own list is
merged with them. `kosherctl site-lists` shows the bundles and switches
them on. In the admin app an approved-sites account is not asked which
kinds of site to block, since it reaches its list and nothing else; the
bundles take that tab's place.

## Developer tools on a filtered machine

Browsers, curl, git and Go trust the filter's certificate through the
system store. The language package managers carry their own certificate
bundles and would refuse every download, which to a student looks like a
broken internet. So the image sets, for every account, the environment
variable each tool honours, pointing at the system bundle, in login shells
and in the desktop session. The registries those tools fetch from are
reachable from every account, including approved-sites ones, because they
hold code rather than pages. [What works](supported.md#developer-tools)
has the table.

Containers fetch as the account, with one wrinkle. A rootless container's
network is pasta, a process owned by the account, so the firewall sees
the account's uid on everything the container sends and dispatches it
as usual. But a process inside the container that is not root there runs
on the host as one of the account's *subordinate* ids, and with
`--network=host` its sockets carry that id. So every rule that names an
account's uid names its subordinate block too (`nft.py`, from
`/etc/subuid` through `containers.py`): the vmap, the proxy redirect,
the extra ports, the video-call switch, the plain resolver. A block is
filtered as its owner, whichever id inside it is talking. kosherd gives
each managed account a block of 65536 when it has none, which is what
lets images with files of many owners unpack at all.

Nix is the one tool that does not fetch as the account. Its daemon runs
as root, and a build that fetches its source (`fetchurl`,
`fetchFromGitHub`) runs as one of the `nixbld` users — below the first
human uid, where the firewall accepts everything. So those users are
dispatched by group before that accept, to a chain of their own
(`nix_build`): the registries and the system domains on 80 and 443, and
`reject`. Which registries a build may reach is the same list every
account has, not the asking account's own, because the firewall cannot
tell whose build it is. And the daemon only answers accounts kosherd has
listed — every managed account whose kind of internet is not "No
internet" — in `/etc/nix/kosheros-users.conf`, which the shipped
`nix.conf` includes last after allowing root alone. The daemon's own
downloads, from the binary caches named in `nix.conf`, run as root and are
not filtered; only root may add a cache.

## Who is connecting: one port per user

Two people at the same machine get different filtering, so the proxy has
to know which account a connection belongs to. nftables already dispatches
every packet by the owning uid, so the daemon gives each filtered account
its own loopback port and redirects that account to it. The proxy reads
the account from the port the connection arrived on: one map lookup,
nothing to race.

A connection the proxy holds no policy for gets the fail-closed floor, the
strict default, never the open web, and is logged as a fault, because every
connection reaching the proxy is a filtered account's by construction.

## Which settings act in which mode

A setting that quietly does nothing for some kind of account is the usual
bug here, so it is pinned by a matrix test rather than by habit. For every
mode, a user is given every setting a non-default value, the real
enforcement files are rendered, and each setting must either turn up in one
of them or be named as deliberately inert with the reason. A new setting
that reaches nothing fails the suite.

In the admin app, a control that still acts in a mode is never greyed out.
"Pictures and video" and "Bad language" stay editable outside filtered
mode because they also decide whether image search results are shown and
whether a bad-language search runs, and their subtitles say which half
applies. YouTube is greyed out everywhere but filtered mode, because
nothing outside the proxy can see which video is playing.
