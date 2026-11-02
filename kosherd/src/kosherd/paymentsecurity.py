"""Hosts a checkout needs that the tracker lists also carry (#265).

A shop does not approve a card on the card number alone. Before the order
is accepted, a script from a fraud service fingerprints the browser and
reports to the shop, and the shop's payment processor or the card's bank
may open a 3-D Secure challenge or a CAPTCHA from a host of its own. Those
scripts look exactly like trackers — they are fingerprinting — so every
tracker list carries them, and so does our `ads` category.

Refusing one does not show an ad. It makes the order fail after the cart
has been consumed, with nothing on the page to say why: Lowe's returned
400 to the final submit and emptied the cart, four times, while the
filter's own activity feed showed nothing blocked. The ad blocker had
answered NXDOMAIN for ThreatMetrix below the proxy, and with ad blocking
switched off the proxy refused the same host at its category step.

So these are never blocked as ads or trackers, in either layer: the DNS
ad blocker (dns.render_adblock, render_category_blocks) leaves them
resolvable, and the proxy's category step lets them through
(apply.write_mitm_rules puts them in each filtered account's exceptions).
Only the category lists step aside: a page from one of these hosts is
still read and scored like any other, and a URL rule can still block it.

An administrator adds the next one with `kosherctl adblock allow HOST`,
guardian-gated like switching the ad blocker off, since each entry is a
host every account can then reach. The built-in entries cannot be removed
here; a URL rule blocks one for an account if a family must.

An entry is a host, or `*.host` for the host and everything under it, the
same as a whitelist entry.
"""
from __future__ import annotations

import re

PAYMENT_SECURITY_HOSTS = (
    # Device fingerprinting the shop consults before it approves a card.
    "*.online-metrix.net",      # ThreatMetrix (LexisNexis): Lowe's, Synchrony, most US banks
    "*.signifyd.com",           # Signifyd
    "*.forter.com",             # Forter
    "*.riskified.com",          # Riskified
    "*.sift.com",               # Sift
    "*.siftscience.com",        # Sift, older tag
    "*.kaxsdc.com",             # Kount data collector
    "*.kount.net",              # Kount
    "*.accertify.net",          # Accertify (American Express)
    "*.iovation.com",           # iovation / TransUnion device reputation
    "*.iesnare.com",            # iovation, collector host
    "*.nudatasecurity.com",     # NuData (Mastercard) behavioural biometrics
    "*.cybersource.com",        # Cybersource device fingerprint and hosted fields
    # The processor's own fraud signals, loaded on the payment form.
    "c.paypal.com",             # PayPal FraudNet
    "m.stripe.com",             # Stripe fraud signals
    "m.stripe.network",         # Stripe fraud signals
    "r.stripe.com",             # Stripe telemetry the payment element requires
    "*.braintreegateway.com",   # Braintree (PayPal) client
    "*.braintree-api.com",      # Braintree GraphQL
    "*.adyen.com",              # Adyen checkout and risk
    # 3-D Secure: the bank's "confirm it is you" frame on the payment page.
    "*.cardinalcommerce.com",   # Cardinal (Visa Secure, Mastercard Identity Check)
    "*.arcot.com",              # Broadcom/Arcot 3-D Secure
    # Bot defence that fronts the shop itself; refused, the shop answers 403.
    "*.perimeterx.net",         # HUMAN / PerimeterX
    "*.px-cdn.net",             # HUMAN / PerimeterX sensor
    "*.px-cloud.net",           # HUMAN / PerimeterX collector
    "*.datadome.co",            # DataDome
    # CAPTCHAs that stand between the cart and the order.
    "*.hcaptcha.com",
    "*.arkoselabs.com",         # Arkose / FunCaptcha
    "*.funcaptcha.com",
)

_ENTRY = re.compile(r"^(\*\.)?([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,}$")


def normalize(entry: str) -> str:
    """An entry as it is stored: lower case, no scheme, path or trailing
    dot. Raises ValueError for something that is not a host."""
    text = (entry or "").strip().lower()
    if "://" in text:
        text = text.split("://", 1)[1]
    text = text.split("/", 1)[0].rstrip(".")
    if not _ENTRY.match(text):
        raise ValueError(f"{entry!r} is not a host name")
    return text


def is_builtin(entry: str) -> bool:
    return entry in PAYMENT_SECURITY_HOSTS


def combined(extra) -> list[str]:
    """The built-in hosts and an administrator's additions, without repeats."""
    return list(dict.fromkeys([*PAYMENT_SECURITY_HOSTS, *(extra or [])]))


def covers(host: str | None, entries) -> bool:
    """Whether `host` is one of `entries`: "example.org" is that host only,
    "*.example.org" the domain and every host under it."""
    name = (host or "").strip().lower().rstrip(".")
    if not name:
        return False
    for entry in entries:
        entry = (entry or "").lower()
        if entry.startswith("*."):
            base = entry[2:]
            if name == base or name.endswith("." + base):
                return True
        elif name == entry:
            return True
    return False
