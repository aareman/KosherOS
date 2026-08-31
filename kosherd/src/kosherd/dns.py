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

from .policy import SAFESEARCH_MODES, Policy

WHITELIST_CONF = "/etc/kosher/dnsmasq.d/whitelist.conf"
SAFESEARCH_CONF = "/etc/kosher/dnsmasq.d/safesearch.conf"

# Search engines publish hostnames that always answer with safe search on.
# Pointing the normal hostname at them is how safe search is enforced for
# users whose traffic we do NOT read (dnsfilter mode); verified against a
# live resolver — dnsmasq follows a cname to an external target.
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
    for target, names in SAFESEARCH_CNAMES.items():
        for name in names:
            lines.append(f"cname={name},{target}")
    return "\n".join(lines) + "\n"
