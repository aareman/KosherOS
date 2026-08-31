"""Render the device policy into an nftables ruleset.

Design (see docs/architecture.md):

- One `table inet kosher`, replaced atomically on every policy change.
- All processes below UID_MIN (system daemons, root, dnsmasq) are unrestricted;
  human users are dispatched by `meta skuid` to a per-mode chain. Unknown human
  UIDs fall through to mode_none — FAIL CLOSED.
- DNS from every human user is redirected to the local dnsmasq (127.0.0.1:53)
  in the nat/output hook, so nobody can pick their own resolver.
- Whitelist mode only allows destinations in the @wl4/@wl6 sets, which dnsmasq
  populates as it resolves whitelisted domains (nftset= directives). Direct-IP
  browsing is therefore blocked for free.
- evasion_block kills DoT (853), QUIC/HTTP3 (udp 443 — also DoH3/ECH), and a
  curated list of DoH-on-tcp-443 provider IPs.
"""

from __future__ import annotations

from .policy import INSPECTED_MODES, UNFILTERED_MODES, Policy

# First human UID. Everything below is trusted system territory.
UID_MIN = 1000

TABLE = "inet kosher"

MODE_CHAINS = {
    "none": "mode_none",
    "whitelist": "mode_whitelist",
    "dnsfilter": "mode_dnsfilter",
    "filtered": "mode_filtered",
    "unfiltered": "mode_unfiltered",
}

# mitmproxy listens here; filtered users' web traffic is redirected to it.
MITM_PORT = 8080

# The KosherOS search front end (the page users get) and the metasearch
# engine behind it. Only the front end filters, so the backend must not be
# reachable by a person: its raw results carry the very snippets the
# filter exists to withhold.
SEARCH_PORT = 8888
SEARCH_BACKEND_PORT = 8889

# The resolver that answers everyone else: family DNS plus safe-search
# redirects. Unfiltered users get a second, plain resolver instead, because
# one machine-wide resolver cannot give different answers per user.
OPEN_DNS_PORT = 5354

# Known DoH-on-tcp-443 resolver IPs (indistinguishable from HTTPS, so blocked
# by address). Curated, shipped with the OS image, portal-refreshed later.
# Cloudflare's own resolvers are listed too: the family filter must be reached
# via *our* dnsmasq on port 53, not via a user's direct DoH connection to the
# unfiltered 1.1.1.1 endpoints.
DEFAULT_DOH_BLOCK4 = (
    "1.1.1.1",
    "1.0.0.1",
    "8.8.8.8/31",       # dns.google
    "8.8.4.4",
    "9.9.9.9",          # quad9
    "149.112.112.112",
    "208.67.222.222",   # opendns
    "208.67.220.220",
    "94.140.14.0/24",   # adguard-dns
    "94.140.15.0/24",
)

LAN4 = ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16")


def _set_block(name: str, addr_type: str, elements: tuple[str, ...] = (), *, interval: bool = False, timeout: bool = False) -> str:
    flags = []
    if interval:
        flags.append("interval")
    if timeout:
        flags.append("timeout")
    lines = [f"    set {name} {{"]
    lines.append(f"        type {addr_type};")
    if flags:
        lines.append(f"        flags {', '.join(flags)};")
    if elements:
        lines.append(f"        elements = {{ {', '.join(elements)} }};")
    lines.append("    }")
    return "\n".join(lines)


def render(policy: Policy, *, dns_uid: int, mitm_uid: int | None = None,
           search_uid: int | None = None,
           doh_block4: tuple[str, ...] = DEFAULT_DOH_BLOCK4) -> str:
    """Return a complete `nft -f`-loadable ruleset for this policy.

    dns_uid: UID dnsmasq runs as — exempt from the port-53 redirect so its
    upstream queries to the family resolver get out.
    """
    vmap_entries = ", ".join(
        f"{u.uid} : jump {MODE_CHAINS[u.mode]}"
        for u in sorted(policy.effective_users(), key=lambda u: u.uid)
    )
    vmap_rule = f"        meta skuid vmap {{ {vmap_entries} }}\n" if vmap_entries else ""

    # Filtered mode: send this user's web traffic into the local mitmproxy,
    # which decrypts it so URL rules can be applied.
    inspected = sorted(u.uid for u in policy.effective_users()
                       if u.mode in INSPECTED_MODES)
    if inspected and mitm_uid is not None:
        uids = ", ".join(str(u) for u in inspected)
        web_redirect = (
            f"        meta skuid {{ {uids} }} tcp dport {{ 80, 443 }} "
            f"redirect to :{MITM_PORT}\n"
        )
    else:
        web_redirect = ""

    # Unfiltered users are sent to the plain resolver, so the safe-search
    # answers the filtered resolver hands out do not reach them.
    unfiltered = sorted(u.uid for u in policy.effective_users()
                        if u.mode in UNFILTERED_MODES)
    if unfiltered:
        uids = ", ".join(str(u) for u in unfiltered)
        open_dns = (
            f"        meta skuid {{ {uids} }} udp dport 53 redirect to :{OPEN_DNS_PORT}\n"
            f"        meta skuid {{ {uids} }} tcp dport 53 redirect to :{OPEN_DNS_PORT}\n"
        )
    else:
        open_dns = ""
    mitm_exempt = f", {mitm_uid}" if mitm_uid is not None else ""
    search_exempt = f", {search_uid}" if search_uid is not None else ""
    # Loopback is otherwise wide open (see the output chain), which is what
    # lets a user reach the search page at all — so the backend is closed
    # by name here rather than left to the general rules.
    backend_guard = (
        f'        oif "lo" tcp dport {SEARCH_BACKEND_PORT} '
        f"meta skuid >= {UID_MIN} reject\n"
        if search_uid is not None else "")

    return f"""#!/usr/sbin/nft -f
# Rendered by kosherd from policy revision {policy.revision}. DO NOT EDIT.

table {TABLE}
delete table {TABLE}

table {TABLE} {{
{_set_block("wl4", "ipv4_addr")}
{_set_block("wl6", "ipv6_addr")}
{_set_block("sys4", "ipv4_addr")}
{_set_block("sys6", "ipv6_addr")}
{_set_block("doh4", "ipv4_addr", doh_block4, interval=True)}
{_set_block("lan4", "ipv4_addr", LAN4, interval=True)}
    # UIDs granted a temporary captive-portal window (element timeout).
{_set_block("captive", "uid", timeout=True)}

    chain dns_redirect {{
        type nat hook output priority dstnat; policy accept;
        # Never redirect the resolver's or the proxy's own traffic, or they
        # would loop back into themselves.
        meta skuid {{ 0, {dns_uid}{mitm_exempt}{search_exempt} }} return
{open_dns}        udp dport 53 redirect to :53
        tcp dport 53 redirect to :53
{web_redirect}    }}

    chain output {{
        type filter hook output priority filter; policy accept;
{backend_guard}        oif "lo" accept
        # Continuation of already-permitted connections (incl. the reply side
        # of inbound ones, e.g. an admin ssh session). A filtered user cannot
        # INITIATE anything with this: their first SYN/datagram is dispatched
        # below and rejected before any conntrack entry goes ESTABLISHED.
        ct state established,related accept
        meta skuid < {UID_MIN} accept
        meta skuid @captive accept
{vmap_rule}        # Unknown human users: fail closed.
        jump mode_none
    }}

    # Inbound: nothing initiates toward this machine except LAN basics.
    # (Closes the "user runs a listener, accomplice connects in" hole —
    # per-UID matching is impossible on input, so inbound is simply shut.)
    chain input {{
        type filter hook input priority filter; policy drop;
        iif "lo" accept
        ct state established,related accept
        ct state invalid drop
        meta l4proto {{ icmp, ipv6-icmp }} accept
        udp dport {{ 68, 546 }} accept
        udp dport 5353 accept
        # Dev convenience; the production image ships with sshd disabled.
        tcp dport 22 accept
    }}

    # LAN-only services every mode keeps: DHCP, mDNS, printing.
    chain local_services {{
        udp dport {{ 67, 68 }} accept
        ip daddr 224.0.0.251 udp dport 5353 accept
        ip6 daddr ff02::fb udp dport 5353 accept
        ip daddr @lan4 udp dport 5353 accept
        ip daddr @lan4 tcp dport {{ 631, 9100 }} accept
    }}

    chain mode_none {{
        jump local_services
        reject
    }}

    chain mode_whitelist {{
        jump evasion_block
        jump local_services
        ip daddr @wl4 tcp dport {{ 80, 443 }} accept
        ip6 daddr @wl6 tcp dport {{ 80, 443 }} accept
        ip daddr @sys4 tcp dport {{ 80, 443 }} accept
        ip6 daddr @sys6 tcp dport {{ 80, 443 }} accept
        reject
    }}

    chain mode_dnsfilter {{
        jump evasion_block
        accept
    }}

    chain mode_filtered {{
        jump evasion_block
        accept
    }}

    # No filtering at all: an adult's own machine. Evasion blocking would be
    # pointless here — there is nothing to evade.
    chain mode_unfiltered {{
        accept
    }}

    chain evasion_block {{
        tcp dport 853 reject
        udp dport 853 reject
        udp dport 443 reject
        ip daddr @doh4 reject
    }}
}}
"""
