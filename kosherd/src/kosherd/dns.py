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

from .policy import Policy

WHITELIST_CONF = "/etc/kosher/dnsmasq.d/whitelist.conf"


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
