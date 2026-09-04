"""Which local user owns a TCP connection.

Neither an intercepted HTTP request nor a connection to a local service
carries the identity of the person who made it, and on a family computer
that identity is the whole point: two people at the same machine get
different filtering. The kernel knows, though — /proc/net/tcp lists the
owning uid of every socket — so a lookup by the client's socket answers
it. This is the same trick `ss -p` uses.

Match on as much of the 4-tuple as the caller knows, and only on LIVE
sockets. The first version matched on local port alone and took the first
line that had it, which is wrong in two ways a browser exercises and a
few curls do not:

- a connection in TIME_WAIT lingers for a minute and reports uid 0, and
  Linux will reuse its port for a fresh connection to a different server,
  so the stale line was found first and the person became "root";
- two live sockets may share a local port when their destinations differ
  (the proxy's own outbound connections included), and port alone cannot
  say which one is the person's.

Either mistake hands the proxy a uid it has no policy for. The proxy now
fails closed on that, but the lookup should not be the thing failing.

Shared by the filtering proxy and the search service so there is one
implementation to get right.
"""

from __future__ import annotations

import ipaddress
import logging

log = logging.getLogger(__name__)

PATHS = ("/proc/net/tcp", "/proc/net/tcp6")

# /proc/net/tcp "st" column. Only these carry a real, current owner.
ESTABLISHED = 0x01
LIVE_STATES = frozenset({0x01, 0x02, 0x03, 0x04, 0x05, 0x08})  # est, syn*, fin-wait, close-wait
TIME_WAIT = 0x06


def _parse_addr(text: str) -> tuple[str | None, int | None]:
    """'0F02000A:C350' -> ('10.0.2.15', 50000); v6 and v4-mapped handled."""
    try:
        hex_addr, hex_port = text.rsplit(":", 1)
        port = int(hex_port, 16)
    except ValueError:
        return None, None
    try:
        if len(hex_addr) == 8:
            # one little-endian 32-bit word
            packed = bytes.fromhex(hex_addr)[::-1]
            return str(ipaddress.IPv4Address(packed)), port
        if len(hex_addr) == 32:
            # four little-endian 32-bit words
            raw = bytes.fromhex(hex_addr)
            packed = b"".join(raw[i:i + 4][::-1] for i in range(0, 16, 4))
            addr = ipaddress.IPv6Address(packed)
            mapped = addr.ipv4_mapped
            return str(mapped if mapped is not None else addr), port
    except (ValueError, ipaddress.AddressValueError):
        pass
    return None, port


def _canonical(addr: str | None) -> str | None:
    if addr is None:
        return None
    try:
        parsed = ipaddress.ip_address(addr.split("%", 1)[0])
    except ValueError:
        return addr
    mapped = getattr(parsed, "ipv4_mapped", None)
    return str(mapped if mapped is not None else parsed)


class UidLookup:
    """Map a client's TCP connection to the uid that owns the socket."""

    PATHS = PATHS

    def _entries(self):
        for path in self.PATHS:
            try:
                with open(path) as fh:
                    next(fh)  # header
                    for line in fh:
                        fields = line.split()
                        if len(fields) < 8:
                            continue
                        laddr, lport = _parse_addr(fields[1])
                        raddr, rport = _parse_addr(fields[2])
                        try:
                            state = int(fields[3], 16)
                            uid = int(fields[7])
                        except ValueError:
                            continue
                        yield laddr, lport, raddr, rport, state, uid
            except OSError:
                continue

    def uid_for_connection(self, local_addr: str | None, local_port: int,
                           remote_addr: str | None = None,
                           remote_port: int | None = None) -> int | None:
        """The uid owning the socket whose LOCAL end is the client's
        (address, port) and, when given, whose remote end is the original
        destination.

        Staged: the exact 4-tuple first, then the client's address and
        port, then the port alone — so a caller that knows more is never
        worse off, and one that knows less still gets the best answer the
        table allows. Live sockets only at every stage: TIME_WAIT never
        counts, and an ESTABLISHED socket beats one still handshaking.
        """
        want_local = _canonical(local_addr)
        want_remote = _canonical(remote_addr)
        live = [e for e in self._entries()
                if e[1] == local_port and e[4] in LIVE_STATES]
        if not live:
            return None

        def pick(candidates):
            if not candidates:
                return None
            candidates.sort(key=lambda e: e[4] != ESTABLISHED)  # established first
            return candidates[0][5]

        if want_local is not None and want_remote is not None and remote_port is not None:
            uid = pick([e for e in live if e[0] == want_local
                        and e[2] == want_remote and e[3] == remote_port])
            if uid is not None:
                return uid
        if want_local is not None:
            uid = pick([e for e in live if e[0] == want_local])
            if uid is not None:
                return uid
        return pick(live)

    def uid_for_port(self, port: int) -> int | None:
        """Port-only lookup for callers that know nothing else. Still skips
        TIME_WAIT and prefers an established socket."""
        return self.uid_for_connection(None, port)
