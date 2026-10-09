# Licensing, and what the lists permit

*No licence is committed yet. The repository has no `LICENSE` file. This
page records the intent, the licence of every list the filter imports, and
the files still to add. It is not legal advice.*

## Intent

The goal is a good operating system for a family: powerful, private and
secure, and easy for a family to configure to its own needs. It is also
meant for people who use Linux, which many developers do and which has not
had a kosher filter. That it costs a family nothing is a consequence of how
it is built rather than the reason for it. The goal is not a business:
there is no product to protect, no revenue to preserve, and no reason to
make anything harder than it needs to be.

Within that, three things at once:

1. **Free and open source.** Nobody should have to pay to protect their
   family.
2. **Nobody takes it closed.** It must not be possible to take this work,
   close the source and sell it.
3. **People can set it up for themselves and for others**, and frum
   developers can contribute.

The third point is easy to get wrong. Someone installing and configuring
KosherOS for another family, for their shul or for a school is a use to
encourage. "Stolen" means somebody closing the source, not a neighbour
helping a neighbour. No licence term and no activation gate should make
third-party setup awkward.

## The licence

**AGPL-3.0-or-later for the code, a Developer Certificate of Origin for
contributions, and a trademark on the name.** Each piece does one job.

| Piece | What it does |
|---|---|
| AGPL-3.0-or-later | nobody can close it, including behind a network service, which the plain GPL would permit |
| DCO, not a CLA | a contributor certifies they wrote what they submitted, in one line of the commit message. No paperwork, no rights assignment |
| trademark on "KosherOS" | a fork may use the code but must rebrand |

AGPL fits the third requirement rather than fighting it. Setting KosherOS
up for other families is permitted outright, and the only obligation that
travels with it is passing along the source, which is a URL.

A contributor licence agreement exists to let the project owner relicense
contributed code, which only matters if there is a proprietary version to
sell. There is not one planned, so a CLA would be friction on exactly the
developers this project wants, in exchange for an option it does not
intend to use.

The trademark protects families rather than revenue. A sloppy or malicious
fork calling itself KosherOS, with the filter quietly weakened, would be
believed by exactly the people least able to check. Owning the name is how
that gets stopped.

A permissive licence (MIT, Apache-2.0) would satisfy the first and third
requirements and fail the second outright: nothing would stand in the way
of a closed derivative.

## The imported lists

Licences were read from each source's own published terms, not from an
aggregator's summary. [Third-party notices](third-party.md) is the full
list with attribution for each.

### The category database: CC BY-SA 4.0

The category database is built from the University of Toulouse blacklists,
published under CC BY-SA 4.0: attribution and share-alike required,
commercial use permitted. What that means here:

- **Attribution** belongs in the product, not only in the source tree. The
  admin app shows the database's sources.
- **Share-alike.** The database is an adaptation, so it ships under CC
  BY-SA 4.0 and anyone may redistribute it, which is in keeping with the
  goal.
- **It does not reach the code.** The licence covers the data; a daemon
  reading a database is not an adaptation of it.
- **It does reach the curated supplement.** The project's own additions in
  `scripts/curated-sites.json` are merged into the same table and travel
  under the same terms. They are a gift to the commons rather than an
  asset.

The Toulouse page also carries, inside an HTML comment, an older metadata
block naming a non-commercial licence. The live licence link supersedes it.
A short email to the maintainer confirming BY-SA would close the question
permanently and is worth sending.

### The ad and tracker lists: fetched one by one

The `ads` category merges fourteen ad and tracker lists, each fetched from
its own maintainer under MIT, CC BY, CC BY-SA, CC0 or an explicit grant on
the maintainer's page. Two well-known lists are left out on purpose: MVPS
hosts and someonewhocares, both non-commercial.

They are left out even though KosherOS is free, because of licence
incompatibility inside the database. The Toulouse data is BY-SA 4.0,
share-alike requires the adaptation to be BY-SA 4.0, and BY-SA 4.0 permits
commercial use. A work cannot be licensed under terms permitting commercial
use while containing material that forbids it, so merging the two lists in
would produce a file that could not be licensed at all. Leaving them out
costs nearly a quarter of the advertising coverage, which is worth knowing
plainly. EasyList is CC BY-SA 3.0 and therefore compatible, though it is in
adblock-filter syntax and would need its own parser; recovering coverage
from it is worthwhile follow-up work.

## Bundled components

The image bundles separate programs that run in their own processes, which
is aggregation rather than derivation, so none of their licences reaches
the daemon's own. The obligations that travel with the binaries still
apply. mitmproxy is MIT. SearXNG is AGPLv3, cloned at build time and run
behind the KosherOS search front end; any modification to SearXNG itself
must be published. PaperWM and ArcMenu are GPL-family GNOME extensions.

The base image is Fedora, so the ISO redistributes a great deal of GPL and
LGPL software; Fedora publishes its own sources, which is ordinarily what
satisfies the offer-of-source obligation. "Fedora" is a trademark of Red
Hat. KosherOS is a Fedora Remix, and Fedora's trademark guidelines govern
the "powered by Fedora" attribution.

## Files still to add

- `LICENSE`, AGPL-3.0-or-later
- SPDX headers across the source tree
- the remaining gaps in the third-party audit, where a component is marked
  "see upstream" rather than with its exact licence
