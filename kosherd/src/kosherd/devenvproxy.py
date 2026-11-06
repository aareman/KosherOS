"""Where devenv's local proxy listens, per account.

devenv (2.3) gives a project's processes names like
http://web.demo.localhost through a reverse proxy of its own, on
127.0.0.1:80 and :443 by default. A port below 1024 makes devenv start it
through sudo, which nobody has here, so `devenv up` with the proxy failed.
devenv reads the addresses from DEVENV_PROXY_LISTEN and
DEVENV_PROXY_HTTPS_LISTEN, and with ports of 1024 or above it binds them
itself: profile.d/kosher-devenv-proxy.sh sets both, to the pair below.

The proxy only ever forwards to 127.0.0.1; it fetches nothing from the
internet, and what the processes behind it fetch goes through the
account's filter like anything else they run. Loopback is open to every
account, so the firewall closes each developer account's pair to the
others (nft.py), as it does Syncthing's web page: a family's other
accounts do not reach somebody's dev servers.

One pair per account, from its uid, clear of every port KosherOS uses
(Syncthing's GUI from 8384, the search pages on 8888/8889, the filter's
listeners from 29999). Two accounts whose uids are 400 apart would share
a pair; a family computer has nowhere near that many.
"""

from __future__ import annotations

BASE = 7100
SLOTS = 400


def ports(uid: int) -> tuple[int, int]:
    """(http, https) for the account."""
    http = BASE + 2 * (int(uid) % SLOTS)
    return http, http + 1
