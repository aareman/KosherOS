"""Render the dnsmasq drop-in that feeds the nftables whitelist sets.

The base dnsmasq config (/etc/kosher/dnsmasq.conf, shipped in the OS image)
binds 127.0.0.1:53 with the Cloudflare family resolvers hardcoded upstream and
includes /etc/kosher/dnsmasq.d/*.conf. kosherd owns exactly one drop-in there:
the whitelist rendering below. As dnsmasq resolves a whitelisted domain it adds
the answer's IPs to the kosher table's wl4/wl6 (or sys4/sys6) sets via its
native nftset support, which is what actually opens the firewall for that
destination.
"""

from __future__ import annotations

import logging

from .policy import INSPECTED_MODES, SAFESEARCH_MODES, Policy

WHITELIST_CONF = "/etc/kosher/dnsmasq.d/whitelist.conf"
SAFESEARCH_CONF = "/etc/kosher/dnsmasq.d/safesearch.conf"
CATEGORY_BLOCK_CONF = "/etc/kosher/dnsmasq.d/categories.conf"

# Search engines publish hostnames that always answer with safe search on.
# Pointing the normal hostname at them is how safe search is enforced for
# users whose traffic we do NOT read (dnsfilter mode); verified against a
# live resolver — dnsmasq follows a cname to an external target.
# The addresses the safe-search hostnames resolve to.
#
# These have to be here, and that is a dnsmasq constraint rather than a
# choice: `cname=` only accepts a target dnsmasq ALREADY knows — from a
# hosts file, from DHCP, or from a host-record. It does not chase the
# target upstream. Without these lines dnsmasq answered www.google.com
# with a CNAME and no address, so safe search did not force safe search,
# it broke Google outright for every filtered account.
#
# Published by the providers and stable for years, but they are data
# rather than code precisely so a portal list update can correct one
# without an OS rebuild.
SAFESEARCH_ADDRESSES = {
    "forcesafesearch.google.com": "216.239.38.120",
    "strict.bing.com": "204.79.197.220",
    "safe.duckduckgo.com": "40.114.177.156",
    "restrictmoderate.youtube.com": "216.239.38.119",
    "restrict.youtube.com": "216.239.38.120",
}

log = logging.getLogger(__name__)

SAFESEARCH_CNAMES = {
    "forcesafesearch.google.com": [
        "google.com", "www.google.com",
        "google.co.il", "www.google.co.il",
        "google.co.uk", "www.google.co.uk",
        "google.ca", "www.google.ca",
    ],
    "strict.bing.com": ["bing.com", "www.bing.com"],
    "safe.duckduckgo.com": ["duckduckgo.com", "www.duckduckgo.com"],
    # Moderate rather than Strict: Strict hides a great deal of ordinary
    # material (including much Torah content), which drives people to turn
    # the filter off entirely.
    "restrictmoderate.youtube.com": [
        "youtube.com", "www.youtube.com", "m.youtube.com",
        "youtubei.googleapis.com", "youtube.googleapis.com",
        "www.youtube-nocookie.com",
    ],
}


def _nftset_line(domain: str, set4: str, set6: str) -> str:
    # dnsmasq nftset syntax: nftset=/example.org/4#inet#kosher#wl4,6#inet#kosher#wl6
    # A dnsmasq domain pattern matches the domain and all its subdomains, which
    # is what our "*." wildcard means; a bare domain should match exactly, but
    # dnsmasq has no exact-only form — accepting subdomains of an approved
    # domain is an acceptable over-match for a family whitelist.
    name = domain.removeprefix("*.")
    return f"nftset=/{name}/4#inet#kosher#{set4},6#inet#kosher#{set6}"


def render(policy: Policy) -> str:
    # Dedupe on the stripped name: "example.org" and "*.example.org" render
    # to the same dnsmasq pattern.
    user_domains = sorted(
        {d.removeprefix("*.")
         for u in policy.effective_users() if u.mode == "whitelist" for d in u.whitelist}
    )
    system_domains = sorted({d.removeprefix("*.") for d in policy.effective_system_whitelist()})

    lines = [
        f"# Rendered by kosherd from policy revision {policy.revision}. DO NOT EDIT.",
        "",
        "# System domains (updates, connectivity check, portal) -> sys4/sys6",
    ]
    lines += [_nftset_line(d, "sys4", "sys6") for d in system_domains]
    lines += ["", "# User whitelists -> wl4/wl6"]
    lines += [_nftset_line(d, "wl4", "wl6") for d in user_domains]
    return "\n".join(lines) + "\n"


def render_safesearch(policy: Policy) -> str:
    """dnsmasq CNAMEs that force safe search, for the filtered resolver.

    This resolver answers everyone except unfiltered users (who are sent to
    a separate plain resolver), because one resolver cannot give different
    answers to different users on the same machine.
    """
    wanted = any(u.mode in SAFESEARCH_MODES for u in policy.effective_users())
    lines = [
        f"# Rendered by kosherd from policy revision {policy.revision}. DO NOT EDIT.",
    ]
    if not wanted:
        lines.append("# No user needs safe search.")
        return "\n".join(lines) + "\n"
    lines.append("# Safe search is forced for every user of this resolver.")
    lines.append("# host-record first: dnsmasq will not resolve a cname "
                 "target it does not already know.")
    for target, address in SAFESEARCH_ADDRESSES.items():
        if target in SAFESEARCH_CNAMES:
            lines.append(f"host-record={target},{address}")
    lines.append("")
    for target, names in SAFESEARCH_CNAMES.items():
        if target not in SAFESEARCH_ADDRESSES:
            # A target with no address would answer a CNAME and nothing
            # else, which does not force safe search — it breaks the site.
            log.error("no address for safe-search target %s; skipping", target)
            continue
        for name in names:
            lines.append(f"cname={name},{target}")
    return "\n".join(lines) + "\n"


def render_category_blocks(policy: Policy, bundle) -> str:
    """dnsmasq entries blocking categorised domains at the DNS layer.

    Filtered users are handled precisely in the proxy, which knows who is
    asking. Users whose traffic is NOT read (dnsfilter, whitelist) can only
    be served by the resolver, and one resolver cannot answer differently
    per user — so this blocks the UNION of what those users block. That is
    a deliberate over-block: with mixed profiles the stricter one wins for
    everybody on this resolver, which is the safe direction to err.
    """
    wanted: set[str] = set()
    for user in policy.effective_users():
        if user.mode in INSPECTED_MODES:
            continue  # the proxy does this one precisely
        if user.mode == "whitelist":
            # Default-deny already: only whitelisted sites resolve to
            # anything reachable, so category blocks add nothing here.
            continue
        wanted |= set(user.blocked_categories)

    # Categories NOT rendered at this layer, however chosen:
    # - adult and malware: the upstream family resolver (Cloudflare
    #   1.1.1.3) already blocks both for every query this resolver
    #   forwards, and the full adult list alone is millions of domains —
    #   far past what a dnsmasq config on a low-end machine can carry.
    #   (This also crashed outright once the sqlite catalogue landed: the
    #   old code walked bundle.domains, which only the JSON seed has.)
    # - the rest are tens of thousands at most, which dnsmasq handles.
    upstream_covered = {"adult", "malware"}
    rendered = wanted - upstream_covered
    if policy.adblock:
        # Already in every resolver through the ad-blocking drop-in
        # (render_adblock); listing it twice would double its memory.
        rendered -= {ADBLOCK_CATEGORY}

    lines = [
        f"# Rendered by kosherd from policy revision {policy.revision}. DO NOT EDIT.",
    ]
    if not wanted:
        lines.append("# No uninspected user blocks a category.")
        return "\n".join(lines) + "\n"
    if wanted & upstream_covered:
        lines.append("# adult/malware are blocked by the upstream family "
                     "resolver for every query.")
    if rendered:
        lines.append(f"# Blocking {', '.join(sorted(rendered))} for users "
                     "the proxy does not see.")
    for domain in bundle.domains_in(rendered):
        # 0.0.0.0 rather than NXDOMAIN: a browser shows a connection error
        # instead of retrying elsewhere.
        lines.append(f"address=/{domain}/0.0.0.0")
        lines.append(f"address=/{domain}/::")
    return "\n".join(lines) + "\n"


# -- ad blocking, for everyone ------------------------------------------------

# Written into BOTH resolvers' drop-in directories: the family one that
# answers filtered accounts and the plain one that answers unfiltered ones.
# That is what makes this a Pi-hole rather than a browser extension — every
# account, every browser, every app, because nftables forces all of the
# machine's DNS through these two processes.
ADBLOCK_CONF = "/etc/kosher/dnsmasq.d/adblock.conf"
ADBLOCK_OPEN_CONF = "/etc/kosher/dnsmasq-open.d/adblock.conf"
# The catalogue category that holds the ad and tracker domains. Fed at image
# build by a Pi-hole grade list on top of the UT1 advertising lists.
ADBLOCK_CATEGORY = "ads"


def render_adblock(policy: Policy, bundle) -> str:
    """dnsmasq entries that make every ad and tracker domain not exist.

    One line per domain, NXDOMAIN (`address=/domain/` with no address): an
    ad that does not resolve is an ad the page never waits for, and half
    the lines of the 0.0.0.0 form the category blocks use — which matters at
    a few hundred thousand domains on a machine with little memory.
    """
    lines = [f"# Rendered by kosherd from policy revision {policy.revision}. DO NOT EDIT."]
    if not policy.adblock:
        lines.append("# Ad blocking is switched off.")
        return "\n".join(lines) + "\n"
    domains = bundle.domains_in({ADBLOCK_CATEGORY})
    lines.append(f"# {len(domains)} advertising and tracker domains, for every account.")
    lines += [f"address=/{domain}/" for domain in domains]
    return "\n".join(lines) + "\n"
