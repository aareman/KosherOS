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

### StevenBlack unified hosts — must be replaced

The source used to be described as "(MIT)". That is true of Steven
Black's own wrapper and scripts and false of the merged `hosts` file that
was actually fetched, which aggregates sixteen upstream lists. Two cannot
be used:

| source | licence | problem |
|---|---|---|
| MVPS hosts | CC BY-NC-SA 4.0 | NonCommercial |
| someonewhocares (Dan Pollock) | "non-commercial with attribution" | NonCommercial |

A third, **yoyo.org**, has no formal licence but turned out to be usable:
its page grants the use in as many words — "Feel free to combine this
list with yours or lists from other sites and put it up on the web,
though!" — and restricts nothing about commercial use, so it can live in
a BY-SA database.

**Why this is still a blocker even though KosherOS is free.** Not because
of the NonCommercial terms themselves — a free distribution could
plausibly satisfy those. Because of **licence incompatibility inside the
database**: the UT1 data is BY-SA 4.0, ShareAlike requires the adaptation
to be licensed BY-SA 4.0, and BY-SA 4.0 *permits commercial use*. A work
cannot be licensed under terms permitting commercial use while containing
material that forbids it. Merging NC rows into this database produces a
file that cannot legally be licensed at all.

Two secondary reasons not to rely on NC material even so: "non-commercial"
has no settled meaning, so donations, a nonprofit preloading machines at
cost, or a shul buying hardware would all be grey areas; and the merged
file gives no way to tell which domains came from which upstream, so the
tainted rows cannot be stripped out later.

**Done** (`fetch-categories.py`, `PLAIN_SOURCES`). The merged file is no
longer fetched; the fourteen compatible upstreams are fetched
individually instead. It was a data change rather than a code change —
the loop already iterated several sources per category and inserts with
`INSERT OR IGNORE`, so overlapping domains across fourteen lists
deduplicate themselves.

**What it cost, measured rather than estimated:**

| | domains |
|---|---:|
| merged file, before | 79,962 |
| fourteen sources, after | 65,746 |
| lost with MVPS and someonewhocares | 18,986 (23.7%) |
| gained by fetching live upstreams | 4,770 |

Nearly a quarter of the advertising coverage goes with those two lists,
which is more than a rounding error and worth knowing plainly. It is
also not a choice: an NC list inside a BY-SA database produces a file
that cannot be licensed at all. Recovering some of that coverage from
another compatible source — EasyList is CC BY-SA 3.0 and therefore
compatible, though it is in adblock-filter syntax and would need a
different parser — is worthwhile follow-up work.

Attribution for all fourteen is recorded in
[THIRD-PARTY.md](third-party.md) and in the database's own `meta`
`source` row, which the admin app displays.

## Other obligations already in the image

Confirmed this session: **mitmproxy** is MIT. Still to confirm as part of
the attribution file: **SearXNG** is AGPLv3 — cloned in the Containerfile
and run as a separate process behind the KosherOS front end, which is
aggregation rather than derivation and so does not reach kosherd, but any
modification to SearXNG itself must be published. **PaperWM** and
**ArcMenu** are GPL-family GNOME extensions. **Fedora's trademark
guidelines** govern how a remix may describe itself, which bears directly
on the "powered by Fedora" branding.

The base image is Fedora bootc, so the ISO redistributes a great deal of
GPL and LGPL software. Fedora publishes its own sources, which is
ordinarily what satisfies the offer-of-source obligation.

## Files still to add

- `LICENSE` — AGPL-3.0-or-later
- `CONTRIBUTING.md` — the DCO and how to sign off a commit
- `THIRD-PARTY.md` — every imported list and bundled component with its
  licence and attribution. Doubles as the audit trail above
- SPDX headers across the source tree
