# Licensing, and what the lists permit

*No licence is committed yet — the repository has no `LICENSE` file. This
records the intent, the audit of what the imported lists actually allow,
and the files still to add. Not legal advice.*

## Intent

The goal is not a business. It is to give the Jewish community the option
of easy, cheap filtered computers **without paying hundreds of dollars
per device per year**, which is what the existing offerings cost. That
sentence decides most of what follows: there is no product to protect,
no revenue to preserve, and no reason to make anything harder than it
needs to be.

Within that, three things at once:

1. **Free and open source for the most part.** Nobody should have to pay
   to protect their family.
2. **Nobody steals it.** A competitor must not be able to take this work
   closed-source and sell it back to the same community at the prices
   this project exists to undercut.
3. **People can set it up for themselves and for others**, and frum
   developers can contribute.

Point three matters and is easy to get wrong. Someone installing and
configuring KosherOS for another family, for their shul or for a school
is a use to *encourage*, not restrict. "Stolen" means a competitor
closing the source, not a neighbour helping a neighbour. No licence term
or activation gate should make third-party setup awkward.

## Recommended setup

**AGPL-3.0-or-later, a DCO for contributions, and a trademark on
"KosherOS".** Each piece does one job:

| piece | what it buys |
|---|---|
| AGPL-3.0-or-later | a competitor cannot close it — including behind a network service, which the plain GPL would permit |
| DCO (not a CLA) | contributors certify they wrote what they submitted. One line in a commit message, no paperwork, no rights assignment |
| trademark on the name | a fork may use the code but must rebrand |

AGPL fits requirement three exactly rather than fighting it: setting
KosherOS up for other families is explicitly permitted, and the only
obligation travelling with it is passing along the source, which is a
URL. What AGPL stops is precisely the case being guarded against — a
company shipping a closed derivative or a hosted service built on it.

**Why a DCO and not a CLA.** A contributor licence agreement exists to
let the project owner relicense contributed code — which only matters if
there is a proprietary version to sell. There isn't one planned, so a CLA
would be pure friction on the frum developers this project wants, in
exchange for an option it does not intend to exercise. A DCO gives the
provenance assurance without asking anyone to sign away anything.

**Why the trademark is still worth registering**, even with nothing to
sell: it protects *families*, not revenue. A sloppy or malicious fork
calling itself KosherOS — with the filter quietly weakened — would be
believed by exactly the people least able to check. Owning the name is
how that gets stopped. Given the audience, this is a stronger reason
than any commercial one.

**Not recommended:** a permissive licence (MIT, Apache-2.0). It satisfies
requirements one and three and fails requirement two outright — this
niche has well-funded incumbents who would face no obstacle at all.

## The imported lists

Audited 2026-09-09 against the sources' own published terms.

### University of Toulouse (UT1) — CC BY-SA 4.0. Usable.

`scripts/fetch-categories.py` imports from
`dsi.ut-capitole.fr/blacklists`. The operative statement on that page is a
`rel="license"` link and badge pointing at **CC BY-SA 4.0**: attribution
and share-alike required, commercial use permitted (which this project
does not need, but which matters below).

Worth knowing: further down the same page, inside an HTML comment and so
not rendered, sits a legacy RDF block declaring `by-nc-sa/4.0` with an
explicit `prohibits CommercialUse`. The live link supersedes stale
commented-out metadata. Since KosherOS is free this is not urgent, but a
short email to Fabrice Prigent confirming BY-SA would close the question
permanently and is worth sending.

What BY-SA requires:

- **Attribution** in the product, not merely in the source tree.
- **ShareAlike.** `categories.sqlite` is an adaptation, so it ships under
  BY-SA 4.0 and anyone may redistribute it. Entirely in keeping with the
  goal.
- **It does not reach the code.** CC BY-SA covers the data; kosherd
  reading a database is not an adaptation of it. The code licence is
  unaffected.
- **It does reach the curated supplement.**
  `fetch-categories.py:262-270` merges `curated-sites.json` into the same
  table as the UT1 rows, so that curation is part of a BY-SA adaptation
  and travels with it. Fine here — it just means the curation is a gift
  to the commons rather than an asset.

### The advertising lists: why two were dropped

The `ads` category is built from fourteen hosts lists fetched
individually, each named with its licence in
[third-party notices](third-party.md). Two lists that ride inside Steven
Black's merged unified-hosts file, MVPS and someonewhocares, are
NonCommercial and are deliberately left out, which is why the merged file
is not fetched whole.

The reason is not the NonCommercial terms themselves; a free distribution
could plausibly satisfy those. It is **licence incompatibility inside the
database**: the UT1 data is BY-SA 4.0, ShareAlike requires the adaptation
to be licensed BY-SA 4.0, and BY-SA 4.0 *permits commercial use*. A work
cannot be licensed under terms permitting commercial use while containing
material that forbids it. Merging NC rows into this database produces a
file that cannot legally be licensed at all. Two secondary reasons not to
rely on NC material even so: "non-commercial" has no settled meaning, so
donations, a nonprofit preloading machines at cost, or a shul buying
hardware would all be grey areas; and a merged file gives no way to tell
which domains came from which upstream, so tainted rows could not be
stripped out later.

Dropping them costs nearly a quarter of the advertising coverage, which is
worth knowing plainly. Recovering some of it from another compatible
source (EasyList is CC BY-SA 3.0, though it is in adblock-filter syntax
and would need a different parser) is worthwhile follow-up work.

## Other obligations already in the image

Every bundled component and its licence is in
[third-party notices](third-party.md). The ones with obligations beyond
attribution: **SearXNG** is AGPLv3, run as a separate process behind the
KosherOS front end, which is aggregation rather than derivation and so does
not reach kosherd, but any modification to SearXNG itself must be
published. **Fedora's trademark guidelines** govern how a remix may describe
itself, which bears directly on the "powered by Fedora" branding. The base
image is Fedora bootc, so the ISO redistributes a great deal of GPL and
LGPL software; Fedora publishes its own sources, which is ordinarily what
satisfies the offer-of-source obligation.

## Files still to add

- `LICENSE`: AGPL-3.0-or-later
- `CONTRIBUTING.md`: the DCO and how to sign off a commit
- SPDX headers across the source tree

`THIRD-PARTY.md` exists and is the attribution file the lists require.
