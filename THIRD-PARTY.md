# Third-party lists and components

Every external list KosherOS imports and every notable component it
bundles, with the licence each is used under. Several of these licences
require attribution, so this file is a condition of use rather than a
courtesy.

Licences were read from each source's own published terms, not from an
aggregator's summary. Last audited **2026-09-10**.

## Filter lists

### Category database

Built by `scripts/fetch-categories.py` into `categories.sqlite`.

| source | licence | used for |
|---|---|---|
| [Université Toulouse Capitole (UT1) blacklists](https://dsi.ut-capitole.fr/blacklists/), maintained by Fabrice Prigent | **CC BY-SA 4.0** | every category — the backbone of the database |

Because the database is an adaptation of BY-SA 4.0 material,
**`categories.sqlite` is itself distributed under CC BY-SA 4.0**, and the
KosherOS curated supplement in `scripts/curated-sites.json` is merged
into it and travels under the same terms.

### Advertising and tracker lists

All land in the `ads` category, which `kosherd/dns.py` turns into
NXDOMAIN for every account.

| source | licence |
|---|---|
| [Steven Black's ad-hoc list](https://github.com/StevenBlack/hosts) | MIT |
| [AdAway](https://adaway.org/) | CC BY 3.0 |
| [add.2o7Net](https://github.com/FadeMind/hosts.extras) | MIT |
| [add.Dead](https://github.com/FadeMind/hosts.extras) | MIT |
| [add.Risk](https://github.com/FadeMind/hosts.extras) | MIT |
| [add.Spam](https://github.com/FadeMind/hosts.extras) | MIT |
| [UncheckyAds](https://github.com/FadeMind/hosts.extras) | MIT |
| [Mitchell Krog's Badd Boyz Hosts](https://github.com/mitchellkrogza/Badd-Boyz-Hosts) | MIT |
| [hostsVN](https://github.com/bigdargon/hostsVN) | MIT |
| [KADhosts](https://kadantiscam.netlify.app/) | CC BY-SA 4.0 |
| [minecraft-hosts](https://github.com/jamiemansfield/minecraft-hosts) | CC0-1.0 |
| [tiuxo hostlist - ads](https://github.com/tiuxo/hosts) | CC BY 4.0 |
| [URLhaus](https://urlhaus.abuse.ch/) (abuse.ch) | CC0 |
| [Peter Lowe's list](https://pgl.yoyo.org/adservers/) | no formal licence; the page grants this use explicitly — "Feel free to combine this list with yours or lists from other sites and put it up on the web, though!" |

### Deliberately not used

These are in StevenBlack's merged unified-hosts file, which is why
KosherOS fetches the fourteen above individually instead of taking that
file whole.

| source | licence | why not |
|---|---|---|
| [MVPS hosts](https://winhelp2002.mvps.org/) | CC BY-**NC**-SA 4.0 | NonCommercial cannot be merged into a CC BY-SA 4.0 database: ShareAlike forces the adaptation to permit commercial use, and NC forbids it |
| [someonewhocares](https://someonewhocares.org/hosts/) (Dan Pollock) | "non-commercial with attribution" | same conflict |

Dropping them costs 18,986 domains, 23.7% of the merged file. It is a
licence constraint, not a tuning choice.

## Bundled components

Separate programs in the image, running in their own processes.
Aggregation rather than derivation, so none of them reaches kosherd's own
licence — but obligations that travel with the binaries still apply.

| component | licence | note |
|---|---|---|
| [Fedora bootc](https://quay.io/fedora/fedora-bootc) | many (GPL, LGPL, MIT, …) | the base image. Fedora publishes the corresponding sources |
| [mitmproxy](https://mitmproxy.org/) | MIT | confirmed |
| [SearXNG](https://github.com/searxng/searxng) | AGPLv3 | run as a separate process behind the KosherOS search front end. **Any modification to SearXNG itself must be published** |
| [PaperWM](https://github.com/paperwm/PaperWM) | GPL-family | GNOME extension, tiling layout |
| [ArcMenu](https://gitlab.com/arcmenu/ArcMenu) | GPL-family | GNOME extension, classic layout |
| [NudeNet](https://github.com/notAI-tech/NudeNet) model | see upstream | the ONNX image detector |
| [niri](https://github.com/YaLTeR/niri) / Noctalia | see upstream | advanced desktop session |

Marked "see upstream" or "GPL-family" where the exact version has not yet
been read at the source. Those are the remaining gaps in this audit.

## Trademarks

"Fedora" is a trademark of Red Hat. KosherOS is a remix, not a Fedora
product, and its use of the name is governed by Fedora's trademark
guidelines — which bear directly on the "powered by Fedora" branding.
