"""Render the dnsmasq drop-in that feeds the nftables whitelist sets.

The base dnsmasq config (/etc/kosher/dnsmasq.conf, shipped in the OS image)
binds 127.0.0.1:53 with the Cloudflare family resolvers hardcoded upstream and
includes /etc/kosher/dnsmasq.d/*.conf. kosherd owns exactly one drop-in there:
the whitelist rendering below. As dnsmasq resolves a whitelisted domain it adds
the answer's IPs to the kosher table's wl4/wl6 (or sys4/sys6, git4/git6) sets via its
native nftset support, which is what actually opens the firewall for that
destination.
"""

from __future__ import annotations

import logging
import re

from .policy import INSPECTED_MODES, SAFESEARCH_MODES, Policy

WHITELIST_CONF = "/etc/kosher/dnsmasq.d/whitelist.conf"
SAFESEARCH_CONF = "/etc/kosher/dnsmasq.d/safesearch.conf"
CATEGORY_BLOCK_CONF = "/etc/kosher/dnsmasq.d/categories.conf"
# The tailnet's names, forwarded to Tailscale's own resolver (render_tailnet).
# In both resolvers, like the ad list: an unfiltered account reaches the
# family's machines by name as much as a filtered one does.
TAILNET_CONF = "/etc/kosher/dnsmasq.d/tailnet.conf"
TAILNET_OPEN_CONF = "/etc/kosher/dnsmasq-open.d/tailnet.conf"
# Where tailscaled answers for the tailnet's names (MagicDNS). Reached by
# dnsmasq, a system user, so the per-account rules never see the query.
TAILNET_RESOLVER = "100.100.100.100"

# Never blocked at the resolver, whatever category lists them. The resolver
# answers root as well as the accounts, so a block here would stop the
# machine's own services, not only the person who chose it. tailscale.com
# is in the "proxy" list, which every supervised account blocks by
# default, and tailscaled cannot sign in or find its relays without it.
# The proxy still blocks the site for a filtered account that blocks the
# category, and nobody but kosherd can drive tailscaled anyway.
RESOLVER_NEVER_BLOCKED = ("tailscale.com",)

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


def _nftset_line(domain: str, *pairs: tuple[str, str]) -> str:
    # dnsmasq nftset syntax: nftset=/example.org/4#inet#kosher#wl4,6#inet#kosher#wl6
    # A dnsmasq domain pattern matches the domain and all its subdomains, which
    # is what our "*." wildcard means; a bare domain should match exactly, but
    # dnsmasq has no exact-only form — accepting subdomains of an approved
    # domain is an acceptable over-match for a family whitelist.
    name = domain.removeprefix("*.")
    sets = ",".join(f"4#inet#kosher#{set4},6#inet#kosher#{set6}" for set4, set6 in pairs)
    return f"nftset=/{name}/{sets}"


def _covers(pattern: str, name: str) -> bool:
    return name == pattern or name.endswith("." + pattern)


def render(policy: Policy) -> str:
    # Dedupe on the stripped name: "example.org" and "*.example.org" render
    # to the same dnsmasq pattern.
    # The account's own list PLUS any ready-made bundles switched on for it
    # (whitelists.py): a parent picks "Torah study" rather than typing the
    # thirty hosts those sites load from.
    from . import whitelists
    from .policy import DEVELOPER_REGISTRIES

    # A whitelist-only account with developer tools on (#123) reaches the
    # package registries through its own list — they are no longer in the
    # system set every account gets.
    user_domains = sorted(
        {d.removeprefix("*.")
         for u in policy.effective_users() if u.mode == "whitelist"
         for d in [*whitelists.effective(u),
                   *(DEVELOPER_REGISTRIES if getattr(u, "developer_tools", False) else ())]}
    )
    system_domains = sorted({d.removeprefix("*.") for d in policy.effective_system_whitelist()})

    # The git hosts developer accounts may reach over SSH (nft.py, #164).
    from .nft import GIT_SSH_HOSTS
    git_domains = sorted(GIT_SSH_HOSTS)

    # One directive per name, naming every set the name belongs in. dnsmasq
    # uses only the single nftset= line with the longest matching domain, so
    # github.com in two lists would have filled one set and not the other,
    # and a whitelisted sub.example.org would have hidden a system-wide
    # example.org from it. A name therefore also carries the sets of every
    # shorter pattern that covers it.
    groups = (
        ("# System domains (updates, connectivity check, portal) -> sys4/sys6",
         ("sys4", "sys6"), system_domains),
        ("# User whitelists -> wl4/wl6", ("wl4", "wl6"), user_domains),
        ("# Git over SSH for developer accounts -> git4/git6", ("git4", "git6"), git_domains),
    )
    lines = [
        f"# Rendered by kosherd from policy revision {policy.revision}. DO NOT EDIT.",
    ]
    done: set[str] = set()
    for heading, _, names in groups:
        lines += ["", heading]
        for name in names:
            if name in done:
                continue
            done.add(name)
            pairs = [pair for _, pair, patterns in groups
                     if any(_covers(p, name) for p in patterns)]
            lines.append(_nftset_line(name, *pairs))
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
        if _never_blocked(domain):
            continue
        # 0.0.0.0 rather than NXDOMAIN: a browser shows a connection error
        # instead of retrying elsewhere.
        lines.append(f"address=/{domain}/0.0.0.0")
        lines.append(f"address=/{domain}/::")
    return "\n".join(lines) + "\n"


def _never_blocked(domain: str) -> bool:
    return any(domain == name or domain.endswith("." + name)
               for name in RESOLVER_NEVER_BLOCKED)


# -- the tailnet --------------------------------------------------------------

# A MagicDNS suffix is a DNS name and nothing else: it goes into a dnsmasq
# line between slashes, so a slash, a space or a newline in it would be a
# second directive.
_SUFFIX = re.compile(r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")


def render_tailnet(suffix: str | None) -> str:
    """dnsmasq lines sending the tailnet's names to tailscaled.

    tailscaled is run with --accept-dns=false (tailnet.py), so it never
    rewrites /etc/resolv.conf: dnsmasq stays the only resolver and every
    account's queries still reach it. MagicDNS keeps answering at
    100.100.100.100 regardless, and this forwards the one tailnet this
    computer is signed in to there. Only that tailnet, not all of ts.net:
    tailscaled forwards a name it does not own back to the system
    resolver, which is this dnsmasq, and a name of someone else's tailnet
    would go round in a loop.
    """
    lines = ["# Rendered by kosherd from the tailnet this computer is signed in to. "
             "DO NOT EDIT."]
    name = (suffix or "").strip().rstrip(".").lower()
    if not name:
        lines.append("# Not signed in to a tailnet.")
    elif not _SUFFIX.match(name):
        log.error("ignoring a tailnet name that is not a DNS name: %r", suffix)
        lines.append("# The tailnet's name was not a DNS name; nothing is forwarded.")
    else:
        lines.append(f"server=/{name}/{TAILNET_RESOLVER}")
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
