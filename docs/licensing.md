# Licensing, and what the lists permit

*No licence is committed yet — the repository has no `LICENSE` file. This
records the intent, the audit of what the imported lists actually allow,
and the files still to add. Not legal advice; the commercial question is
worth a few hundred dollars of real counsel before taking money.*

## Intent

Three things at once, in the maintainer's own framing:

1. **Free and open source for the most part.** The OS itself is free.
   Nobody should have to pay to protect their family.
2. **Nobody steals it.** A competitor must not be able to take this work
   closed-source and sell it back to the same community.
3. **People can set it up for themselves and for others**, and frum
   developers can contribute.

Point three matters and is easy to get wrong. Someone installing and
configuring KosherOS for another family, for their shul or for a school
is a use to *encourage*, not restrict. "Stolen" means a competitor
closing the source, not a neighbour helping a neighbour. No licence term
or activation gate should make third-party setup awkward.

Alongside these, the right to build a sellable product later — most
likely around the filter — is to be kept open rather than exercised now.

## The licence is not what protects any of this — copyright control is

The ability to relicense later depends on holding rights to *all* the
code, not on the licence text. The moment a contribution lands under a
copyleft licence with no agreement in place, that code can never be
relicensed without tracking its author down years later. The option
closes quietly and permanently, which is why contribution intake is
load-bearing rather than paperwork.

## Recommended setup

**AGPL-3.0-or-later, plus a lightweight CLA, plus a trademark on
"KosherOS".** Each piece does one job:

| piece | what it buys |
|---|---|
| AGPL-3.0-or-later | a competitor cannot close it — including behind a network service, which the plain GPL would let them do |
| CLA | contributors grant a broad relicensing right, so the sellable-product option stays open. One signature, and it is the only thing that keeps that door open |
| trademark on the name | a fork may use the code but must rebrand. Cheapest, strongest commercial protection there is, and it works under any licence |

AGPL fits requirement three exactly rather than fighting it: setting
KosherOS up for other families is explicitly permitted, and the only
obligation that travels with it is passing along the source, which is a
URL. What AGPL stops is precisely the case being guarded against — a
company shipping a closed derivative or a hosted service built on it.

**The alternative, if contribution flow matters more than optionality:**
AGPL-3.0 with a DCO and no CLA. Maximum contributor comfort, nothing can
be stolen, but dual-licensing becomes impossible once the first outside
patch lands. Worth choosing deliberately, not by default.

**Not recommended:** a permissive licence (MIT, Apache-2.0). It satisfies
requirements one and three and fails requirement two outright — this
niche has well-funded incumbents who would face no obstacle at all.

## Where the moat actually is

Not in the data, as it turns out. The category database is derived from
CC BY-SA material and must ship under BY-SA regardless of what the code
says (below). So what remains genuinely proprietary is the tzniut
classifier and other models — original work, not derived from any
imported list — together with the hosted service, the brand and support.
This makes the AGPL + CLA choice easier, not harder: little is being
given up that was not already open.

## The imported lists

Audited 2026-09-09 against the sources' own published terms.

### University of Toulouse (UT1) — CC BY-SA 4.0. Usable, including commercially.

`scripts/fetch-categories.py` imports from
`dsi.ut-capitole.fr/blacklists`. The operative statement on that page is a
`rel="license"` link and badge pointing at **CC BY-SA 4.0**: commercial
use permitted, attribution and share-alike required.

**One wrinkle to close.** Further down the same page, inside an HTML
comment and therefore not rendered, sits a legacy RDF block declaring
`by-nc-sa/4.0` with an explicit `prohibits CommercialUse`. The live link
supersedes stale commented-out metadata, but it is exactly the artifact
an opposing lawyer finds. Before taking money, email Fabrice Prigent and
get BY-SA confirmed in writing.

What BY-SA costs:

- **Attribution** in the product, not merely in the source tree.
- **ShareAlike.** `categories.sqlite` is an adaptation, so it ships under
  BY-SA 4.0 and anyone may redistribute it, commercially included.
- **It does not reach the code.** CC BY-SA covers the data; kosherd
  reading a database is not an adaptation of it. The code licence is
  unaffected.
- **It does reach the curated supplement.**
  `fetch-categories.py:262-270` merges `curated-sites.json` into the same
  table as the UT1 rows, so that curation becomes part of a BY-SA
  adaptation. Keeping it proprietary would mean holding it in a separate
  file consulted at runtime and never merging it at build time.

### StevenBlack unified hosts — not usable as merged. This is a blocker.

`fetch-categories.py:103` describes the source as "(MIT)". That is true
of Steven Black's own wrapper and scripts and false of the merged `hosts`
file actually fetched, which aggregates sixteen upstream lists. By the
project's own readme, three cannot be used here:

| source | licence | problem |
|---|---|---|
| MVPS hosts | CC BY-NC-SA 4.0 | **NonCommercial** |
| someonewhocares (Dan Pollock) | "non-commercial with attribution" | **NonCommercial** |
| yoyo.org | not specified | unknown |

NonCommercial is incompatible with a paid product *and* with BY-SA — NC
material cannot be merged into a BY-SA database either. So as built
today the `ads` category makes the shipped database both unsellable and
internally inconsistent.

**The fix is small.** Pull the compatible upstreams directly instead of
the merged output: Steven Black's own list, hostsVN, Badd Boyz and the
four FadeMind extras plus UncheckyAds (MIT); AdAway (CC BY 3.0); tiuxo
(CC BY 4.0); KADhosts (CC BY-SA 4.0); minecraft-hosts and URLhaus (CC0).
Three of sixteen dropped, nearly all coverage kept. It is a change to
`PLAIN_SOURCES` and a longer attribution file. Confirm each licence at
its own repository rather than trusting the aggregator's summary.

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
ordinarily what satisfies the offer-of-source obligation, but the
redistribution question belongs in the counsel conversation.

## Files still to add

- `LICENSE` — the chosen licence
- `CONTRIBUTING.md` — the CLA or DCO, and how to sign it
- `THIRD-PARTY.md` — every imported list and bundled component with its
  licence and attribution. Doubles as the audit trail above
- SPDX headers across the source tree
