"""Which local user owns a TCP connection.

Neither an intercepted HTTP request nor a connection to a local service
carries the identity of the person who made it, and on a family computer
that identity is the whole point: two people at the same machine get
different filtering. The kernel knows, though — /proc/net/tcp lists the
owning uid of every socket — so a lookup by the client's source port
answers it. This is the same trick `ss -p` uses.

Shared by the filtering proxy and the search service so there is one
implementation to get right.
"""

from __future__ import annotations

PATHS = ("/proc/net/tcp", "/proc/net/tcp6")


class UidLookup:
    """Map a local TCP source port to the uid that owns the socket."""

    PATHS = PATHS

    def uid_for_port(self, port: int) -> int | None:
        for path in self.PATHS:
            try:
                with open(path) as fh:
                    next(fh)  # header
                    for line in fh:
                        fields = line.split()
                        try:
                            local_port = int(fields[1].rsplit(":", 1)[1], 16)
                        except (IndexError, ValueError):
                            continue
                        if local_port == port:
                            try:
                                return int(fields[7])
                            except (IndexError, ValueError):
                                return None
            except OSError:
                continue
        return None
