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

SSH, FTP, SMTP, POP3 and IMAP (ports 22, 21, 25, 110 and 143) are the
exception. In these the server speaks first, so the proxy would have nothing
to read and the program would wait forever. They are not sent to the proxy
and are refused at once, so the program says the connection was refused.
On any other port, the proxy closes a connection if the program has sent
nothing after ten seconds.

Three things do not go through the proxy, because they are not web and the
proxy could not read them:

- **Mail.** IMAPS, SMTPS and submission are allowed directly.
- **Extra ports** an administrator grants one account, under *Beyond the
  web* on the account's page, for a program that is neither web nor mail.
  The filter's own ports, and Syncthing's (see [Syncthing](#syncthing)), can
  never be granted this way. Empty by default.
- **SSH to other machines**, under that heading on the account's page: the
  machines an account may connect to over SSH — a name, an address, a
  range, or `tailnet` for every machine on this computer's tailnet — and
  nothing else. Port 22 is otherwise closed, because an SSH tunnel reaches
  anything. *Anywhere* opens it to every machine, for an account an
  administrator trusts with that, and the switch says so.
- **Video calls.** Zoom, Meet and school calls send picture and sound over
  UDP to high ports, which cannot be inspected. That is allowed by default
  so calls work without a visit to the admin app. The switch on the
  account's page says that this is the one channel the filter cannot read.
  Below port 1024 an account gets only NTP, and QUIC stays refused.

A *Basic protection* account has no proxy, so for it the web is ports 80
and 443 to anywhere, plus the same named protocols. *Approved sites only*
and *No internet* allow nothing beyond the approved sites.

The package registries developer tools fetch from are reachable from every
account, and the image points each tool at the filter's certificate;
[what works](supported.md#developer-tools) has the table.

### Developer tools

Nix, devbox and devenv are on every KosherOS machine, but an account may use
them itself only when an administrator switches **Developer tools** on for it
(the account's Apps tab; guardian-gated). Off — the default — the Store is the
only way to install: kosherd runs catalogue installs as root into the account's
own profile, and the account cannot run Nix at all. The `nix` program and its
libraries can be read only by root and the group `kosher-nix`, which kosherd
keeps to the accounts with the switch on. The Nix daemon refuses everyone
else too, but that alone is not enough: Nix can download and build into a
store in the account's home without the daemon. Turning the switch on or off
reaches a terminal the next time the person signs in. On, the account
can build anything from nixpkgs from a terminal and reaches the package
registries (npm, PyPI, crates.io, GitHub and the rest) through its own filter
mode. Those registries are no longer in the set every account reaches: a
whitelist-only account without the switch sees exactly its list.

The switch also lets the account use git over SSH with GitHub, GitLab,
Codeberg and Bitbucket, so `git clone git@github.com:...` and `gh repo clone`
work. Port 22 is open to those four hosts only, and SSH to any other server is
refused, because an SSH connection can carry a tunnel around the filter. For a
git server of the family's own, an administrator can add port 22 under *Extra
ports*, which opens SSH to every server for that account.

What the switch cannot stop is a copy of Nix the account downloads itself.
nix-portable, for one, is a single file on GitHub that brings its own Nix
and needs no user namespaces, and the binary cache it fetches from
(cache.nixos.org and its mirrors) is an ordinary site to the filter. That is
the same as any other program downloaded and run from the home folder, which
KosherOS does not prevent today. Everything such a program fetches still goes
through the account's own filter.

A build's own downloads — `fetchurl`, `fetchFromGitHub` — run as the Nix build
users, which belong to no account. Their web goes through the proxy on a
listener of their own, with bare addresses and encrypted-DNS tricks refused and
no picture scoring (a build fetches tarballs, not pages). Everything else of
theirs is refused, including loopback, so a build can never be a relay for the
account that started it. The daemon's own fetches from the binary cache run as
root, from caches only root can name.

What a build may download is the strictest filter among the accounts that have
developer tools on, since only they can start one. Every one of their own
rules and blocked categories applies, so on a machine with one developer
account a build gets exactly that account's filter, and any site that filter
allows is fine. If no account has developer tools on, a build downloads
nothing; the Store's own installs come from the binary cache.

If one of the developer accounts is on approved sites only, a build may
download only from known source hosts: GitHub and its download hosts, GitLab,
Codeberg, Bitbucket and SourceHut, the SourceForge, GNU and kernel.org mirrors,
the project download servers nixpkgs names in its mirror list, the language
registries (PyPI, crates.io, npm, Go's module proxy, RubyGems, Hackage, Maven
Central), NixOS's own servers, and that account's own approved sites. An
administrator can add a host to this list with `kosherctl build-sources add
HOST` (guardian-gated), and `kosherctl build-sources` shows it. The list is
used only in this case. The built-in part is in `kosherd/buildsources.py`.

When the proxy refuses a build's download, the build log names the host and
the account whose setting refused it.

### Syncthing

Syncthing keeps folders the same on a person's computers and phone. The filter
cannot read what it sends: paired devices talk to each other over encrypted
connections that only they can open, so files that arrive from a paired device
are not checked by the filter at all. What can be controlled is who has it and
where it may connect.

Syncthing is off on a new computer, and the Store does not offer it. An
administrator turns it on for the computer in **Admin → Advanced**. Turning it
off again stops Syncthing for every account. Each account's install and synced
folders are kept, and its Syncthing starts again if it is turned back on.

An administrator's account and an unfiltered one can install Syncthing from the
Store like any other app. Any other account needs an administrator to allow it
for that account, on the account's Apps tab, even when the account may use the
whole store. Once it is allowed, Syncthing runs for that account in the
background, and the Syncthing icon opens its page in the browser.

Only Syncthing itself gets its ports. It runs as a system service for the
account, and the firewall lets ports 22000 and 22067 (and UDP 22000 and 21027)
out only for that service. Any other program on the account, including a copy
of Syncthing started from a terminal, is refused on those ports. To find the
person's other devices, the service also reaches Syncthing's discovery servers
and relay list directly. Taking the permission away stops the service and
closes the ports; the synced folders stay where they are.

Devices on the same home network cannot connect to it unless an administrator
switches on **Let devices on this network connect directly** for the account.
With it on, the computer accepts Syncthing's connections from addresses on the
local network only, never from the internet. Approving Syncthing, taking it
away, and that switch are all recorded in the activity log.

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

Video-call services are in no category a parent can switch off. The Toulouse
lists file Microsoft Teams and Skype as chat, which would make a meeting link
open on the block page for any account that blocks social networks. Teams,
Skype, Zoom and Google Meet are listed as calls instead, so a meeting link
opens for a filtered account. Their pages and pictures are still read and
judged like any other site's.

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

### What the page shows after it arrived

A chat message, a feed item, a search result fetched as JSON and drawn by
script never passes through the proxy as text. So every page the proxy
lets through carries a small script of the proxy's own, served on a
reserved address on the page's own origin (`/__kosheros__/filter.js`,
cached for a day) and answered by the proxy, never forwarded. In the page
it does two things. The language filter: the same word list, as the same
patterns, over every text node the page adds or changes, replaced in
place, or in block mode the proxy's block page. And the content scorer:
the words the page adds are gathered and sent back to be judged with the
same tolerance that judged the page itself, so a page that renders what
the filter would have blocked is blocked a moment later. What a person
types is theirs and is left alone; code on the page is not prose and is
skipped. A page whose Content-Security-Policy names a script nonce gets
the nonce on the tag; one that forbids every script, its own included,
keeps the proxy's judgement of the HTML and nothing more.

The tag carries a token for that one page, signed for the account and the
site, and the script sends it back with every call. The proxy answers the
word list and the judge only to a call that carries it, refuses a block
reported under another site's address, and allows a page so many calls a
minute. A block the script reported is marked in the activity log as what
the page showed after it loaded, so a parent can tell it from a page the
proxy refused on the way in.

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

## Calmer sites

X, Reddit, YouTube and Facebook cannot always be blocked wholesale — an
article links a tweet you need to read, a reddit post answers a question
about a bug — but their feeds are built to be bottomless. An account can
be given a calmer version of a site instead. A feed page ends after a set
number of items; a post opened by a link is shown on its own, without the
comments, the related videos or the rails back into the site; and the
ads, sidebars and recommendations are never drawn. Signing in is never
touched, so a flow that needs the site's own login still works.

The pages are single-page applications, so the stripping happens in the
page: the proxy injects a script of its own (`/__kosheros__/calm.js`),
and the script re-reads the address on every soft navigation, because a
click inside these apps does not load a new page. The rules for which
page is a post and which a feed live in one place and are handed to the
script, so the two cannot drift.

A site may also carry a daily budget. The page sends a heartbeat while
it is visible; the proxy keeps the seconds, writes them out so a proxy
restart (every policy change restarts it) does not hand the time back,
and answers the page with what is left. A budget that is spent closes
that site for the rest of the day with the proxy's own page; another
calmed site is unaffected. The count is a nudge rather than a wall — a
page could misreport its own seconds — the wall is the category block.

The calmer sites are set on the person's Calm tab, and apply in Filtered
internet mode, the only mode where the proxy sees the page.

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

## Usage

The admin app's Usage page shows how much went through the filter and how
much it stopped, as numbers: pages and files loaded, data downloaded and
uploaded, pages, pictures, videos and searches blocked, and how long the
filter held each request (the average, the time half of all requests
finished within, and the time 95 in 100 finished within). It covers today,
this week, this month or all time, for everyone or for one person, and
each person's overview shows today's figures. Only accounts on filtered
internet are counted, because on the other settings the filter never sees
the pages.

Only totals are kept, never an address. The proxy adds up in memory and
writes its totals once a minute, the search service writes a line for each
search it refuses, and kosherd folds both into a file of daily totals. Days
are kept for sixty days and then dropped, after they have been added to
an all-time total, so the file stays under a megabyte however long
the computer runs. A writer stops writing if kosherd stops collecting, and
keeps counting in memory until it can.

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
as "nothing new". `python3 scripts/publish-lists.py https://portal.example` sends this
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
