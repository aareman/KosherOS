# Content filtering

A filtered account's traffic is judged at three depths, each only where the
one before it cannot answer. The resolver answers by **domain**: Cloudflare's
family DNS and the category database say what a site is before a connection
is made. For an account in *Filtered internet* the proxy then reads the
**address**, so one corner of a site can be refused without the rest, and
the **page itself**, so a site nobody has catalogued is judged by its words,
bad language is replaced on a page that is otherwise fine, and pictures and
video are handled as [media filtering](media-filtering.md) describes. Search
results pass through the same policy; see [search](search.md).

Reading the page means decrypting that account's HTTPS on this machine. The
admin app says so beside the mode, and the certificate that makes it
possible is checked by the [filter's own health report](#saying-when-the-filter-is-not-filtering).

## The category database

The image imports the University of Toulouse blacklists at build time:
10.5 million domains across 24 categories (4.6 million of them adult), stored
as SQLite at 190 MB. A lookup answers in a few microseconds with about 60 MB
resident; a Python dict of the same data would cost hundreds of megabytes of
RAM, which the family machine does not have.

The build spot-checks the list against sites that matter: chinuch.org,
torahanytime.com, artscroll.com, ou.org, chabad.org, sefaria.org,
hebrewbooks.org, yeshiva.co, matzav.com, theyeshivaworld.com, kosher.com,
wikipedia.org and several government and medical sites all come back clean,
while pornhub, xvideos, onlyfans, chaturbate, stripchat, tinder, bet365,
nordvpn and hidemyass are all caught. A list that blocked chinuch.org would
be worse than no list, so that check runs before shipping.

The database refreshes without an OS release, through the signed manifest
described under [lists](#lists-shipped-portal-and-the-familys-own-edits).
Its licence, and the licences of the advertising lists merged into it, are in
[third-party notices](third-party.md).

## Which ports an account may use

A supervised account's network is **default-deny**, not "everything except
what is blocked". For an account in *Filtered internet* every TCP connection
it opens, on any port, is redirected into that account's own proxy
listener, so a web page served on 8080 or 8443 meets the same URL rules,
category lists and picture filter as one on 443. Loopback is never
redirected: a program on the computer talking to itself is nobody's business.
Anything that reaches the proxy and is not HTTP, or TLS carrying HTTP, is
refused rather than passed through (`rawtcp=false`).

Three things do not go through the proxy, because they are not web and the
proxy could not read them:

- **Mail.** IMAPS (993), SMTPS (465) and submission (587) are allowed
  directly. They are named protocols with a reason, not "ports the web might
  use".
- **Extra ports** an administrator grants one account, under *Beyond the
  web* on the account's page (or `kosherctl network UID --extra-ports 22`).
  This is the advanced escape hatch for a program that is neither web nor
  mail: ssh, say. The filter's own ports (53, 80, 443) can never be granted
  this way. Empty by default.
- **Video calls.** UDP cannot be inspected at all, and Zoom, Meet and school
  calls send picture and sound over UDP to high ports. That is allowed by
  default, so calls work without a visit to the admin app, and the switch on
  the account's page says plainly that this is the one channel the filter
  cannot read; turn it off for an account that has no calls to make. Below
  port 1024 an account gets only NTP; QUIC (UDP 443) stays refused.

*Basic protection* accounts have no proxy, so for them the web is 80 and 443
to anywhere plus the same named protocols; other TCP is refused. *Approved
sites only* and *No internet* allow nothing beyond the approved sites.

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
queue as a blue banner under the header of whichever page is open, and each
request is answered with one button on its row. A request carries no
authority of its own; nothing changes until an admin says so, which is
exactly what lets the asking be frictionless.

**Nobody has to ask first.** Everything the filter blocks, hides or
refuses is written to an activity log (`kosherd/activity.py`; the proxy
and the search service append to their own files under
`/var/lib/kosher-activity`, and kosherd is the only reader). The admin app
shows it as the Activity page, newest first, narrowable to one person, and
on each person's own page as "Blocked today". A blocked page has an Allow
button that does exactly what approving a request does (`AllowUrl`), so a
parent who sees the block can fix it before the child comes to ask. It is a
record of the filter, not of the person: what was allowed through is never
written, so it cannot become a browsing history. Settings changes are in the
same diary with the admin who made them, so a two-parent household can see
who changed what. Everything is trimmed to a week.

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
the domain on its whitelist; an allow rule would do nothing there, since
its traffic never reaches the proxy. A filtered account gets an allow rule
placed *ahead* of the existing rules, because one added after whatever
blocked the page would never be reached. The admin chooses "just this page"
or "all of this site"; a whitelist account is only offered the site, since
that is all its enforcement can express.

## Saying when the filter is not filtering

Every list loader fails open and quiet. Miss the category database and
the bundle is empty; miss the content terms and the scorer matches nothing;
miss the word list and the profanity filter is off. That behaviour is right,
because a missing file must not take the machine down, and the silence is
the part that would be wrong: the account would still say "Filtered
internet" while nothing it covers was filtered.

`kosherd/selfcheck.py` counts what actually loaded and reports anything
empty or truncated in words a person can act on. A truncated list is the
worse case of the two, because it looks like it is working.

`FilterStatus` gathers that with three other facts about the machine right
now, as opposed to what is configured:

* which services should be running and are not: only the ones this
  household's modes actually need, because a warning that does not matter
  teaches people to ignore warnings;
* whether pictures are being checked, or hidden because the machine cannot
  keep up, or hidden because no model is installed (the proxy publishes
  `checking`, `too_slow` or `no_model`; see [media filtering](media-filtering.md#measured-on-the-machine-that-matters));
* whether the inspection certificate is generated, in the trust store, and
  the same in both places.

That last one is worth the space. Without a trusted CA, every HTTPS page a
filtered account opens shows a certificate warning: the person sees a
broken internet and a scary security error, and nothing connects that to
the filter. A stale anchor (the CA regenerated without refreshing the
trusted copy) gives the identical symptom and is harder to spot, so it is
checked by comparing the two files rather than by their existence.

All of it appears in the admin app as an amber count beside Protection in
the sidebar, with the full list first on the Protection page. When nothing
is wrong the badge is absent and the page says "Running", because a healthy
machine that says nothing is indistinguishable from one that was never
checked. `kosherctl status` prints the same, `kosherctl lists` prints what
each list holds and where it came from, and `kosherctl log -g youtube` (or
any pattern) prints the filter services' recent journal lines through
kosherd, since there is no root on the machine to run `journalctl` with.

## Lists: shipped, portal, and the family's own edits

Every list ships complete and is meant to stay that way. Three layers
decide what is in force:

    shipped   what came with the image
    portal    a signed update, so a list improves without an OS rebuild
    edits     this family's handful of additions and removals

The portal layer *replaces* the shipped copy, and the family's delta is
applied on top of whichever base is in force, so a portal update brings
every new entry and still keeps a parent's edits. Overrides live in
`/var/lib/kosher-lists`, a world-readable directory of nothing but lists,
because the proxy and the search service run unprivileged and must be able
to read them; a test asserts no list is ever looked for under the private
state directory, and `just check-services` proves the proxy picks an
override up.

**The portal bundle** is signed with the same Ed25519 key as the policy and
replay-protected the same way, because rolling a family back to last year's
word list is an attack rather than a downgrade. Only four filenames are
accepted, by name: a portal that could choose the filename could write
something that is not a list into a directory the filter reads. The bundle
is the same for every enrolled device; what a family changes stays on their
own machine. A portal that predates lists returns 404, which is treated as
"nothing new" rather than an error.

**The category database is a manifest, not a payload.** Five million
domains is 190 MB, which cannot ride inside a signed JSON document, so the
bundle carries a manifest (a version, a URL, a size and a SHA-256) and the
device fetches the file itself. The signature covers the hash, so the
download needs no trust of its own: it can come from a CDN, a mirror or
plain HTTP, and a byte out of place is caught on arrival. The hash is
computed as the bytes stream in, so a mismatch costs one wasted download and
never a wasted disk. The file then has to prove it is a catalogue before it
is installed: it is opened, queried, and refused if it holds fewer than a
hundred thousand domains. The old catalogue stays in place through any
failure.

**Publishing.** `just publish-lists https://portal.example` sends this
repository's lists to a portal, so a family on an older image catches up
without waiting for a new one. Add `--catalog-url` to publish a
category-database manifest alongside them; the hash is taken from the
**local** file rather than from whatever the URL returns at that moment,
which is the mistake this would otherwise invite.

**An edit is a delta, not a copy.** What a family will actually do is
disagree with a handful of entries. If that edit were a copy of the whole
list, the family would silently stop receiving every later improvement to
it, so an override is `{"add": ..., "remove": [...]}` applied on top of
whatever ships; a complete replacement is still honoured, because the portal
may legitimately send one. Three lists are editable this way: the
bad-language list, the blocked search terms, and the words pages are judged
by. The shop rules and the category database are not, because neither is a
thing to hand somebody a text box for. `EditList` caps an edit at a hundred
entries, and the refusal says why: the answer to needing more is that the
shipped list is not finished and should be fixed for everybody.
`GetListEdits` reports how many entries ship alongside how many the family
has changed, because "119 ship with KosherOS, you have added two" is the
sentence that stops somebody trying to build the list themselves.

From the command line: `kosherctl words show words`,
`kosherctl words add words blast --replacement bother`,
`kosherctl words remove searches "bikini atoll"`.

## Other languages

The word lists ship in sixteen languages (English, Hebrew, Yiddish, Russian,
Ukrainian, French, Spanish, Portuguese, German, Italian, Dutch, Hungarian,
Polish, Arabic, Persian and Turkish), chosen for where Jewish families
live. Each is one file under `os-image/lists/`, and the three shipped files
are built from them (`just lists`); a test fails when they drift. The README
there says how to add a word or a language and, more importantly, what to
leave out.

The matchers (`kosherd/textfold.py`) know what the lists need:

* A **word is a whole word in any script**, and in Hebrew and Arabic also
  behind the prefixes those languages glue on: והביקיני counts as
  ביקיני, البورن as بورن, while מזונות is left alone. This cuts both
  ways, which is why זונות is not on the list: with a prefix allowed,
  the bracha is מ plus the plural of a curse.
* **Vowel points and accents between letters are ignored**, and a mark
  before a letter is the middle of a word, not a boundary.
* **An accented letter matches its plain spelling** (cabrón and cabron,
  kurwa mać and kurwa mac) for the accents that mark stress, and only
  those: ñ is not n (coño, cono), ö is not o (göt, got). ß is ss, ё is е,
  the alef variants are one letter, and the Persian kaf is the Arabic
  one, so one list serves both.
* A word under four letters is matched as written, and a disguised spelling
  must keep half its letters, so a digit standing in for a letter cannot
  make 8.5 read as *bs* or 455 as *ass*.
* The bad-language list compiles **one pattern per script** and only runs
  the ones a page contains; the alternation is factored by first letter,
  so fifteen hundred words scan a page in about the time a hundred and
  fifty did.
* A page is scored in its **folded** spelling, so a term is listed once.
  A Hebrew phrase may take the article on its second word (נערות
  הליווי), an Arabic one on both.
* The "writing about the problem" register that halves a page's evidence
  speaks the same languages.

Every Latin-script list shares one matcher, so an entry that is an ordinary
word in a neighbouring language (*hoe* in Dutch, *hell* in German, *git* in
Turkish, *mayo* anywhere) rewrites innocent pages. `scripts/list-sweep.py`
(`just sweep`), which CI runs weekly and on every change to the lists or
the matchers, fetches curated ordinary sites in every language, the top of
the web minus the adult category for breadth, and a fresh sample of the
adult category for recall, judges each as the proxy would, and fails the
run when ordinary pages read as explicit too often, a language's pages are
rewritten wholesale, or the adult sample slips past the scorer. The report
names the ordinary pages the lists reacted to and never an adult one.

Two limits remain, and both are recorded rather than hidden. A word two
Latin-script languages spell alike gets one replacement (*puta* reads as
Spanish on a Portuguese page), because nothing tells the proxy what
language a page is in; reading `<html lang>` would fix that. And the
scorer's plural rule is English (*bra* matches *bras*, which is French for
arms), so the other lists carry their own forms.

## YouTube

Restricted Mode is one switch for the whole site, which is useless for a
family that wants shiurim but not entertainment. So there are three
levers, and the important part is *where* they are applied.

**Per category.** YouTube labels every video with one of fifteen
categories, and a parent can turn any of them off: block Entertainment
and Gaming, keep Education. The settings keep a category's id (`10`);
YouTube's player JSON names it (`Music`, and `Howto & Style` where the
table says How-to); the proxy maps the name to the id, however it is
spelled, and reads the microformat's category rather than the first
`"category"` key it meets.

**Shorts.** Not a category (a short can be in any of them) but a kind of
video all the same, and the one no category limit reaches. It is a switch
of its own in the same list, on by default. Off means the `/shorts` pages
are refused, the reel endpoints and the ordinary player are answered
"cannot be played" when the page asking is a short, and the Shorts shelves
and reel links are taken out of the feeds.

**Thumbnails.** At the modesty picture levels YouTube's thumbnails
(`i.ytimg.com`) are treated as image-search thumbnails are: small pictures
of the whole web from a host the family cannot curate, hidden whenever a
person is in them rather than judged one by one at a size the detector
cannot judge reliably.

**Per channel.** An approved-channel list, which when it has anything in
it is the whole allowance: only those channels play. A channel is approved
by searching for it by name in the admin app.

**Restricted Mode**, sent as a header on every request and forced at the
resolver, as a floor under all of it.

### It has to be enforced in the app, not on the page

YouTube is a single-page application. The first load is HTML; after that,
every video is fetched as JSON from `/youtubei/v1/player` and no watch page
is ever parsed again. A filter that reads only the HTML therefore checks
the first video a child opens and nothing they click afterwards, which is
almost all of them.

So the rules are applied to the player API, on the site's own hosts and on
`youtubei.googleapis.com`, where the clients call the same endpoints, and
the block is returned in YouTube's own "cannot be played" shape rather than
as a block page. That response is consumed by the player, not read by a
person: a 403 spins forever, and an HTML page where JSON was expected is a
broken app.

### What a person sees, and why it is not always the same frame

A video opened by its address is a page, and gets the KosherOS block page
with its "ask for this page" form. A video clicked inside the app is a
player answer, and gets YouTube's own error frame. The frame differs
because the request differs; the **words are the same**: "Blocked by
KosherOS", then the reason and "you can ask the administrator of this
computer for it. Nothing changes until they say yes." Both are written to
the activity log either way.

A third message is not ours at all: with Restricted Mode on, YouTube
blocks some videos by itself and says so in its own words. Nothing on this
machine can make that one look like ours, which is the price of having a
floor under the per-account rules.

### The preview that plays on hover

Hovering a thumbnail plays the video: the feed carries an inline player
and a moving thumbnail, and neither asks the player API whether it may.
For any account with YouTube limits those keys are taken out of the feed
(keys, not entries, so the video keeps its picture and its title and only
the part that plays on hover goes), and the animated preview picture
itself (`an_webp` on the thumbnail host) is hidden if a client asks for one
anyway.

### The feeds, for approved-channel accounts only

An account limited to a few channels would otherwise get a home page full
of videos that all fail to play, which teaches a child that the computer
is broken rather than that somebody chose this. So the feed responses are
pruned to the channels that will actually play.

Deliberately conservative: an entry is dropped only if it both looks like
a video renderer **and** names a channel, so anything with an unfamiliar
shape survives untouched. The channel is matched by shape (the id, the
handle, or the byline text), not by searching the response for a name,
because a title mentioning an approved channel is not that channel's
video. Nothing is pruned for accounts that are not limited to a channel
list; playback filtering already covers those, and rewriting a feed
nobody needed rewritten is all risk and no benefit.

## Ad blocking, for everyone

Like a Pi-hole, and for the same reason: the machine already forces every
account's DNS through its own two resolvers (the family one that answers
filtered accounts, the plain one that answers unfiltered ones; see
`kosherd/nft.py`), so a domain list rendered into both reaches every
browser and every app on every account. A browser extension covers one
browser for one account and can be switched off; this cannot be stepped
around from inside a session, and Firefox is told (`DNSOverHTTPS` locked
off) not to carry its DNS elsewhere.

What is blocked is the catalogue's `ads` category: UT1's advertising lists
plus the fourteen hosts lists named in [third-party notices](third-party.md),
about sixty-five thousand domains. `kosherd/dns.py` writes them as
`address=/domain/` (NXDOMAIN: one line each, and an ad that does not resolve
is an ad the page never waits for) into both resolvers' configuration. A
filtered account that also blocks the `ads` category is not given the list
twice.

Machine-wide, on by default. It is not content filtering, so it applies to
unfiltered accounts too; switching it **off** is the guardian-gated act
(`SetAdBlock`, the switch on the Protection page under "Applies to
everyone", `kosherctl adblock off`), because an ad network is also where
immodest imagery arrives uninvited. It sits there rather than on a
person's page because one resolver cannot answer differently per account,
and the page says so next to the switch. What it does not do: cosmetic
filtering (an empty box where an ad was), first-party ads served from the
site's own domain, and ads inside YouTube's own player, which the YouTube
limits handle.

## Which settings act in which mode

Twice the interesting bug has been a setting that quietly did nothing for
some kind of account. So it is a matrix test rather than a habit of
checking: for every mode, a user is given every setting a non-default
value, the real enforcement artefacts are rendered, and each setting must
either turn up in one of them or be named as deliberately inert *with the
reason*. A new setting that reaches nothing fails the suite.

Two consequences show in the admin app. The search service's policy file
carries only what the account's mode can use (an unfiltered or no-internet
account is one word), so the file cannot be read as evidence that settings
are applied which are not. And "Pictures and video" and "Bad language" are
editable in every mode that enforces anything, with subtitles that say which
half applies: the media level also decides whether image search results are
shown at all, and the language setting also blocks searches containing bad
language. YouTube stays greyed out everywhere but filtered mode, and that
one is genuine: nothing outside the proxy can see which video is playing.

## Ready-made approved-site lists

"Approved sites only" is the strongest thing this product offers and the
most work to set up: Sefaria alone loads from half a dozen hosts, and a page
whose fonts and scripts are blocked looks *broken* rather than blocked,
which is how a filter ends up switched off. So the image ships bundles
(`os-image/files/usr/share/kosher/whitelist-bundles.json`,
`kosherd/whitelists.py`) that a parent switches on instead of typing
domains:

- **Torah study**: Sefaria, YUTorah, TorahAnytime, the Daf Yomi sites,
  Chabad.org, HebrewBooks, OU Torah, Alhatorah, Mechon Mamre and the rest,
  with the audio and asset hosts they use.
- **Email and files**: Gmail, Outlook, OneDrive, Google Drive, Dropbox,
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

In the admin app, a whitelist account is **not** asked which kinds of
site to block: it reaches its list and nothing else, so the category grid
would be fifteen toggles that change nothing. The approved lists take
their place on that tab.

## Developer tools, containers and Nix

The user-facing table of which tools work, and the environment that makes
each trust the filter's certificate, is on [what works](supported.md#developer-tools).
This is how the traffic is dispatched.

The registries the tools fetch from (npm, yarn, bun, PyPI, astral,
RubyGems, crates.io, deno.land, JSR, the Go proxy, GitHub release assets)
are in `DEVELOPER_REGISTRIES` (`policy.py`), reachable from every account
like the OS's own update and Flathub hosts. They hold code, not pages, and a
school or work project on this computer needs them on day one.

**Containers fetch as the account**, with one wrinkle. A rootless
container's network is pasta, a process owned by the account, so the
firewall sees the account's uid on everything the container sends and
dispatches it as usual. But a process inside the container that is not root
there runs on the host as one of the account's *subordinate* ids, and with
`--network=host` its sockets carry that id. So every rule that names an
account's uid names its subordinate block too (`nft.py`, from `/etc/subuid`
through `containers.py`): the vmap, the proxy redirect, the extra ports,
the video-call switch, the plain resolver. A block is filtered as its
owner, whichever id inside it is talking. kosherd gives each managed
account a block of 65536 when it has none, which is what lets images with
files of many owners unpack at all.

**Nix is the one tool that does not fetch as the account.** Its daemon runs
as root, and a build that fetches its source (`fetchurl`,
`fetchFromGitHub`) runs as one of the `nixbld` users, below the first human
uid, where the firewall accepts everything. So those users are dispatched
by group before that accept, to a chain of their own (`nix_build`): the
registries and the system domains on 80 and 443, and `reject`. Which
registries a build may reach is the same list every account has, not the
asking account's own, because the firewall cannot tell whose build it is.
And the daemon only answers accounts kosherd has listed (every managed
account whose kind of internet is not "No internet") in
`/etc/nix/kosheros-users.conf`, which the shipped `nix.conf` includes last
after allowing root alone. The daemon's own downloads, from the binary
caches named in `nix.conf`, run as root and are not filtered; only root may
add a cache.

## Who is connecting: one port per user

The proxy has to know which person a connection belongs to, because two
people at the same machine get different filtering. nftables already knows,
since it dispatches every packet by the owning uid (`meta skuid`), so
kosherd gives **each filtered user their own loopback port** (30000,
30001, … in uid order) and redirects them to it. The proxy listens on
exactly that set and reads the user from the port a connection arrived on.
One map lookup, deterministic, nothing to race.

The alternative, scanning `/proc/net/tcp` for the client's source port,
was tried first and failed under a browser's connection churn: a
just-closed connection lingers in TIME_WAIT reporting uid 0, Linux reuses
its port for a new connection to a different server, and the scan found
the ghost first, so a child became "root", a uid with no policy, and got
the open web. The `kosherd.uidmap` lookup still exists (the search service
uses it, matching the full 4-tuple on live sockets only), and the proxy
falls back to it only for a rules file that predates per-user ports.

"Who is at the keyboard" would not have been enough either: GNOME's Switch
User leaves the previous session running, and a browser left open there
keeps making requests under its own uid while somebody else is active.
The owner of the socket is the only right answer, and the port carries it.

Whatever the identification, a uid the proxy holds no policy for gets the
fail-closed floor (the default categories, immodest media, substituted
language), never the open web. Every connection reaching the proxy is a
filtered user's by construction, so an unknown one is a fault to log, and
it is logged, not a stranger to wave through.
