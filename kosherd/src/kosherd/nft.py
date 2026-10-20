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
- The supervised modes are DEFAULT-DENY (issue #33, F1). A filtered account's
  every TCP port is redirected into its own proxy listener — web on 8080 is
  still web — except the resolver's 53, the named non-web protocols
  (DIRECT_TCP_PORTS: mail), the protocols where the server speaks first
  (SERVER_FIRST_TCP_PORTS: ssh and the like, which would hang in the
  proxy) and the account's own extra_ports; what still reaches the mode
  chain is refused. An account with developer tools on may also use SSH
  to the major git hosts (GIT_SSH_HOSTS, the @git4/@git6 sets). dnsfilter, which has no proxy, allows
  80/443 and the same named protocols and refuses other TCP. Loopback is
  never redirected. UDP cannot be inspected, so it is a decision: NTP, and
  high ports for video calls (UserPolicy.video_calls, on by default).
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
MITM_PORT = 8080  # legacy single port; kept for tools that still name it

# One loopback port PER FILTERED USER, so the proxy knows who is connecting
# from the port it accepted on. Before this, every user was redirected to
# one port and the proxy worked the owner out by scanning /proc/net/tcp for
# the client's source port — a lookup a browser's connection churn could
# get wrong (a TIME_WAIT ghost, two sockets sharing a port), and a wrong
# owner meant the wrong policy. nftables already dispatches by skuid; the
# port simply carries that decision to the proxy. Deterministic, per boot
# and across it, with nothing to race.
MITM_PORT_BASE = 30000
# The Nix build users' own listener (#123). A fixed-output build fetches as
# nixbld, on behalf of an account the firewall cannot name, so its web goes
# through the proxy as a supervised identity of its own — categories, bare
# addresses and DoH blocked, no picture scoring — rather than to an
# address set nobody can keep honest. Below the per-user ports, fixed.
BUILDER_PORT = 29999


def mitm_ports(policy: Policy) -> dict[int, int]:
    """uid -> the proxy port that user's web traffic is redirected to.

    Sequential from MITM_PORT_BASE in uid order. Rendered into the nft
    ruleset, the proxy's rules file and its listener list from this one
    function, so the three can never disagree.
    """
    inspected = sorted(u.uid for u in policy.effective_users()
                       if u.mode in INSPECTED_MODES)
    return {uid: MITM_PORT_BASE + i for i, uid in enumerate(inspected)}

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

# What a supervised account may reach that is not the web and so cannot go
# through the proxy: mail. IMAPS, SMTPS and submission — named protocols
# with a reason, not "ports the web might use". Everything else that is
# TCP goes to the proxy (filtered) or is refused (dnsfilter); an account
# that genuinely needs more gets it in `extra_ports`, an advanced setting.
DIRECT_TCP_PORTS = (465, 587, 993)
# Protocols where the SERVER speaks first: the client waits for a greeting
# before it sends a byte. mitmproxy picks a connection's protocol from the
# client's first bytes, so in the proxy these hang with neither side
# moving (#164: `gh repo clone` over SSH sat there forever). They are not
# redirected; they reach the mode chain, which refuses them at once, so
# the program says "Connection refused" instead of waiting. ftp, ssh,
# smtp, pop3, imap. extra_ports still opens any of them for an account.
SERVER_FIRST_TCP_PORTS = (21, 22, 25, 110, 143)
# Git over SSH for accounts with developer tools on, to these hosts only.
# dnsmasq adds their addresses to @git4/@git6 as they resolve (dns.py), as
# it does for the whitelists. Port 22 is never open to any host: `ssh -D`
# to a server of one's own is a tunnel around the filter. A git server of
# the family's own is the administrator's call, through extra_ports.
GIT_SSH_HOSTS = ("github.com", "gitlab.com", "codeberg.org", "bitbucket.org")
GIT_SSH_PORT = 22
# UDP cannot be inspected at all. Below this a port is a named service and
# the account gets only NTP; above it is where video calls live, and that
# is a per-account switch (UserPolicy.video_calls), on by default.
UDP_HIGH = 1024
UDP_LOW_ALLOWED = (123,)


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


def _ports(*ports: int) -> str:
    return ", ".join(str(p) for p in sorted(set(int(p) for p in ports)))


def _extras(user) -> tuple[int, ...]:
    return tuple(getattr(user, "extra_ports", ()) or ())


def render(policy: Policy, *, dns_uid: int, mitm_uid: int | None = None,
           search_uid: int | None = None, build_gid: int | None = None,
           subids: dict[int, list[tuple[int, int]]] | None = None,
           doh_block4: tuple[str, ...] = DEFAULT_DOH_BLOCK4) -> str:
    """Return a complete `nft -f`-loadable ruleset for this policy.

    dns_uid: UID dnsmasq runs as — exempt from the port-53 redirect so its
    upstream queries to the family resolver get out.
    build_gid: the Nix build users' group (nixbld). A fixed-output build
    (fetchurl, fetchFromGitHub) runs as one of them with the network, on
    behalf of whichever account asked and outside that account's filter.
    Their 80/443 is redirected into the builders' proxy listener
    (BUILDER_PORT), their loopback is otherwise refused — a build must not
    be a relay for the account that started it — and nothing else leaves.
    subids: each account's subordinate id blocks ({uid: [(start, end)]},
    containers.py). A process inside a rootless container that is not
    root there runs on the host as one of these ids, so every rule that
    names the account names its blocks too: the container is filtered as
    its owner.
    """
    subids = subids or {}

    def owner(uid: int) -> list[str]:
        """The uid and its blocks, as nft set elements."""
        return [str(uid)] + [f"{s}-{e}" for s, e in subids.get(uid, [])]

    def owner_match(uid: int) -> str:
        elems = owner(uid)
        return elems[0] if len(elems) == 1 else "{ " + ", ".join(elems) + " }"

    # Before the system-uid accept: nixbld is below UID_MIN too.
    nix_build = (
        f"        meta skgid {build_gid} jump nix_build\n"
        if build_gid is not None else "")
    # The builders' web goes to their listener; everything else of theirs on
    # loopback is refused, ahead of the loopback accepts below — a build
    # that could listen on 127.0.0.1 would be an unread relay for the
    # account that started it, and would reach the search backend and the
    # plain resolver, which the >= UID_MIN guards do not cover.
    build_redirect = (
        f"        meta skgid {build_gid} tcp dport {{ 80, 443 }} redirect to :{BUILDER_PORT}\n"
        if build_gid is not None else "")
    build_loopback = (
        f"        meta skgid {build_gid} ip daddr 127.0.0.0/8 tcp dport {BUILDER_PORT} accept\n"
        f'        meta skgid {build_gid} oif "lo" reject\n'
        f"        meta skgid {build_gid} ip daddr 127.0.0.0/8 reject\n"
        f"        meta skgid {build_gid} ip6 daddr ::1 reject\n"
        if build_gid is not None else "")
    vmap_entries = ", ".join(
        f"{elem} : jump {MODE_CHAINS[u.mode]}"
        for u in sorted(policy.effective_users(), key=lambda u: u.uid)
        for elem in owner(u.uid)
    )
    vmap_rule = f"        meta skuid vmap {{ {vmap_entries} }}\n" if vmap_entries else ""

    # Filtered mode: send this user's web traffic into the local mitmproxy,
    # which decrypts it so URL rules can be applied.
    ports = mitm_ports(policy)
    by_uid = {u.uid: u for u in policy.effective_users()}
    if ports and mitm_uid is not None:
        # One rule per user: the destination port IS the user's identity to
        # the proxy (see mitm_ports). EVERY TCP port is redirected, not
        # only 80 and 443: web on 8080 or 8443 is still web, and a port
        # that is not redirected is a port that is not read. The
        # exceptions are 53 (the resolver's, redirected above), the named
        # non-web protocols, the server-first ones (refused in the mode
        # chain rather than left to hang in the proxy), and the account's
        # own extra ports.
        web_redirect = "".join(
            f"        meta skuid {owner_match(uid)} tcp dport != {{ {_ports(53, *DIRECT_TCP_PORTS, *SERVER_FIRST_TCP_PORTS, *_extras(by_uid.get(uid)))} }} "
            f"redirect to :{port}\n"
            for uid, port in ports.items())
    else:
        web_redirect = ""

    # Per-account exceptions, before the mode dispatch: the extra ports an
    # administrator granted, and the accounts whose video calls are off.
    supervised = [u for u in policy.effective_users() if u.mode in ("filtered", "dnsfilter")]
    per_user = "".join(
        f"        meta skuid {owner_match(u.uid)} tcp dport {{ {_ports(*u.extra_ports)} }} accept\n"
        for u in supervised if u.extra_ports)
    # Git over SSH to the major hosts for the accounts with developer
    # tools on — to those addresses only, never port 22 at large.
    # Whitelist accounts too: the hosts are the same few, and git is what
    # developer tools are for.
    git_ssh = sorted(u.uid for u in policy.developer_accounts()
                     if u.mode not in UNFILTERED_MODES)
    if git_ssh:
        elems = ", ".join(e for uid in git_ssh for e in owner(uid))
        per_user += (
            f"        meta skuid {{ {elems} }} ip daddr @git4 tcp dport {GIT_SSH_PORT} accept\n"
            f"        meta skuid {{ {elems} }} ip6 daddr @git6 tcp dport {GIT_SSH_PORT} accept\n")
    no_video = sorted(u.uid for u in supervised if not u.video_calls)
    if no_video:
        elems = [e for uid in no_video for e in owner(uid)]
        per_user += (f"        meta skuid {{ {', '.join(elems)} }} "
                     f"udp dport >= {UDP_HIGH} reject\n")

    # Unfiltered users are sent to the plain resolver, so the safe-search
    # answers the filtered resolver hands out do not reach them.
    unfiltered = sorted(u.uid for u in policy.effective_users()
                        if u.mode in UNFILTERED_MODES)
    if unfiltered:
        uids = ", ".join(e for uid in unfiltered for e in owner(uid))
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
    # The plain resolver on 5354 answers unfiltered accounts and nobody else
    # (#127). Loopback is never redirected and nothing guarded that port, so
    # a dns-filtered account could ask it directly and get unfiltered
    # answers. Matched by loopback ADDRESS, not interface: an unfiltered
    # account's own queries arrive here redirected, with 127.0.0.1 as their
    # destination but the original route's interface (see below).
    open_dns_guard = "".join(
        f"        meta skuid {owner_match(uid)} ip daddr 127.0.0.0/8 {proto} dport {OPEN_DNS_PORT} accept\n"
        for uid in unfiltered for proto in ("udp", "tcp")
    ) + "".join(
        f"        ip daddr 127.0.0.0/8 {proto} dport {OPEN_DNS_PORT} meta skuid >= {UID_MIN} reject\n"
        f"        ip6 daddr ::1 {proto} dport {OPEN_DNS_PORT} meta skuid >= {UID_MIN} reject\n"
        for proto in ("udp", "tcp")
    )

    return f"""#!/usr/sbin/nft -f
# Rendered by kosherd from policy revision {policy.revision}. DO NOT EDIT.

table {TABLE}
delete table {TABLE}

table {TABLE} {{
{_set_block("wl4", "ipv4_addr")}
{_set_block("wl6", "ipv6_addr")}
{_set_block("sys4", "ipv4_addr")}
{_set_block("sys6", "ipv6_addr")}
    # The git hosts' addresses (GIT_SSH_HOSTS), filled by dnsmasq.
{_set_block("git4", "ipv4_addr")}
{_set_block("git6", "ipv6_addr")}
{_set_block("doh4", "ipv4_addr", doh_block4, interval=True)}
{_set_block("lan4", "ipv4_addr", LAN4, interval=True)}
    # UIDs granted a temporary captive-portal window (element timeout).
{_set_block("captive", "uid", timeout=True)}

    chain dns_redirect {{
        type nat hook output priority dstnat; policy accept;
        # Loopback is nobody's business but the machine's: a dev server or a
        # local service on any port — 443 included — is never redirected.
        oif "lo" return
        ip daddr 127.0.0.0/8 return
        ip6 daddr ::1 return
        # Never redirect the resolver's or the proxy's own traffic, or they
        # would loop back into themselves.
        meta skuid {{ 0, {dns_uid}{mitm_exempt}{search_exempt} }} return
{build_redirect}{open_dns}        udp dport 53 redirect to :53
        tcp dport 53 redirect to :53
{web_redirect}    }}

    chain output {{
        type filter hook output priority filter; policy accept;
{build_loopback}{backend_guard}{open_dns_guard}        oif "lo" accept
        # A packet the nat rules just redirected (DNS to the resolver, web to
        # the proxy) already has a loopback DESTINATION here, but this hook
        # still sees the interface of its original route — so `oif "lo"`
        # above misses it. Matched by address, or every redirected query and
        # page would fall through to the per-mode reject below. It did.
        ip daddr 127.0.0.0/8 accept
        ip6 daddr ::1 accept
        # Continuation of already-permitted connections (incl. the reply side
        # of inbound ones, e.g. an admin ssh session). A filtered user cannot
        # INITIATE anything with this: their first SYN/datagram is dispatched
        # below and rejected before any conntrack entry goes ESTABLISHED.
        ct state established,related accept
{nix_build}        meta skuid < {UID_MIN} accept
        meta skuid @captive accept
        # LAN basics every mode keeps: DHCP, mDNS, printing.
        jump local_services
{per_user}{vmap_rule}        # Unknown human users: fail closed.
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
        reject
    }}

    chain mode_whitelist {{
        jump evasion_block
        ip daddr @wl4 tcp dport {{ 80, 443 }} accept
        ip6 daddr @wl6 tcp dport {{ 80, 443 }} accept
        ip daddr @sys4 tcp dport {{ 80, 443 }} accept
        ip6 daddr @sys6 tcp dport {{ 80, 443 }} accept
        reject
    }}

    chain nix_build {{
        # A Nix build fetching its source. Its 80 and 443 were redirected to
        # the builders' proxy listener above and accepted on loopback; what
        # still arrives here is anything else, and a build needs nothing
        # else. (The daemon's own downloads from the binary cache run as
        # root, from substituters only root can name.)
        jump evasion_block
        reject
    }}

    chain mode_dnsfilter {{
        # No proxy here, so the web is 80 and 443 to anywhere, plus the
        # named non-web protocols; other TCP is refused, not left unread.
        jump evasion_block
        meta l4proto {{ icmp, ipv6-icmp }} accept
        tcp dport {{ 80, 443, {_ports(*DIRECT_TCP_PORTS)} }} accept
        udp dport {{ {_ports(*UDP_LOW_ALLOWED)} }} accept
        udp dport >= {UDP_HIGH} accept
        reject
    }}

    chain mode_filtered {{
        # Every TCP port this account opens was redirected into its proxy
        # listener (dns_redirect) and accepted on loopback above, except the
        # named non-web protocols, which pass here directly. Anything else
        # reaching this chain is refused: default-deny, by construction.
        jump evasion_block
        meta l4proto {{ icmp, ipv6-icmp }} accept
        tcp dport {{ {_ports(*DIRECT_TCP_PORTS)} }} accept
        udp dport {{ {_ports(*UDP_LOW_ALLOWED)} }} accept
        udp dport >= {UDP_HIGH} accept
        reject
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
