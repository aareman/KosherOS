"""Checks shared by the transparent web listeners.

Basic and approved-sites modes judge HTTP Host and TLS SNI. They never
terminate TLS, so encrypted HTTP authorities/domain fronting remain outside
that mode's guarantees; full filtering is the mode that reads requests.
"""

from __future__ import annotations

import ipaddress
import re
import socket
from urllib.parse import unquote


def hostname(value: str | None) -> str:
    """A canonical DNS name, or empty for an absent, invalid or literal name."""
    if not isinstance(value, str):
        return ""
    value = value.lower().rstrip(".")
    try:
        value = value.encode("idna").decode("ascii").rstrip(".")
    except UnicodeError:
        return ""
    # libc accepts integer, short, octal and hexadecimal IPv4 spellings
    # that ipaddress intentionally rejects. They are addresses, not names.
    try:
        socket.inet_aton(value)
        return ""
    except OSError:
        pass
    try:
        ipaddress.ip_address(value.strip("[]"))
        return ""
    except ValueError:
        pass
    if len(value) > 253 or not all(
        re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
        for label in value.split(".")
    ):
        return ""
    return value


def host_in(host: str, names) -> bool:
    """Exact names, or an explicitly wildcarded domain and its subdomains."""
    host = host.lower().rstrip(".")
    for name in names:
        pattern = str(name).lower().rstrip(".")
        if pattern.startswith("*."):
            base = pattern[2:]
            if base and (host == base or host.endswith("." + base)):
                return True
        elif pattern and host == pattern:
            return True
    return False


def nonpublic_address(value: str) -> bool:
    """The upstream proxy must not act as a relay to local/reserved networks."""
    address = ipaddress.ip_address(value.split("%", 1)[0])
    if getattr(address, "ipv4_mapped", None) is not None:
        address = address.ipv4_mapped
    return not address.is_global


def doh_request(path: str, headers) -> bool:
    """Encrypted DNS by HTTP shape, independent of an endpoint's name."""
    path = unquote(path.split("?", 1)[0]).lower().rstrip("/")
    if path.endswith("/dns-query"):
        return True
    for header in ("content-type", "accept"):
        value = headers.get(header, "")
        if any(part.split(";", 1)[0].strip().lower() == "application/dns-message"
               for part in value.split(",")):
            return True
    return False


def normalize_lan_hosts(hosts) -> list[str]:
    """Explicit web access to named LAN devices; no wildcards, URLs or ports.

    An ordinary site allow rule is not this permission: the administrator
    must separately approve that name reaching private addresses.
    """
    if not isinstance(hosts, (list, tuple)) or len(hosts) > 128:
        raise ValueError("name at most 128 home-network devices")
    result = []
    for entry in hosts:
        if not isinstance(entry, str):
            raise ValueError("a home-network device must be a hostname or private address")
        value = entry.strip().lower().rstrip(".")
        try:
            address = ipaddress.ip_address(value.strip("[]"))
        except ValueError:
            name = hostname(value)
            if not name or "*" in value or name == "localhost" or name.endswith(".localhost"):
                raise ValueError(f"{entry!r}: use one hostname or private address, without a path, port or wildcard")
        else:
            if address.is_global or local_service_address(str(address)):
                raise ValueError(f"{entry!r}: use a private device address")
            name = str(address)
        if name not in result:
            result.append(name)
    return result


def lan_permitted(host: str, names) -> bool:
    """Exact independent device permission, including canonical IPv6."""
    try:
        name = str(ipaddress.ip_address(host.strip("[]")))
    except ValueError:
        name = hostname(host)
    return bool(name) and name in names


def local_service_address(value: str) -> bool:
    """Loopback is the daemon boundary, never a grantable LAN destination."""
    address = ipaddress.ip_address(value.split("%", 1)[0])
    if getattr(address, "ipv4_mapped", None) is not None:
        address = address.ipv4_mapped
    return address.is_loopback or address.is_unspecified or address.is_multicast


def fetch_range(url: str, peer, headers: dict, start: int, end: int, *, lan: bool = False) -> bytes:
    """Fetch only from the validated connection's numeric peer, without redirects.

    The media worker must not perform a second DNS lookup or follow a new
    destination outside server_connect's checks. TLS still verifies the
    original hostname; cookies and proxy-environment settings are not used.
    """
    import http.client
    import ssl
    from urllib.parse import urlsplit

    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.username is not None:
        raise OSError("invalid video source")
    port = parts.port or (443 if parts.scheme == "https" else 80)
    if not peer or len(peer) < 2 or peer[1] != port:
        raise OSError("video source has no validated peer")
    try:
        address = str(ipaddress.ip_address(peer[0]))
    except ValueError as exc:
        raise OSError("video peer is not a numeric address") from exc
    if local_service_address(address) or (nonpublic_address(address) and not lan):
        raise OSError("video source is not an approved network destination")
    if start < 0 or end < start:
        raise OSError("invalid video range")

    class PinnedConnection(http.client.HTTPConnection):
        default_port = 443 if parts.scheme == "https" else 80

        def connect(self):
            self.sock = socket.create_connection((address, port), timeout=self.timeout)
            if parts.scheme == "https":
                try:
                    self.sock = ssl.create_default_context().wrap_socket(
                        self.sock, server_hostname=parts.hostname)
                except BaseException:
                    self.sock.close()
                    raise

    connection = PinnedConnection(parts.hostname, port, timeout=10)
    try:
        path = parts.path or "/"
        if parts.query:
            path += "?" + parts.query
        connection.request("GET", path, headers={**headers, "Range": f"bytes={start}-{end}"})
        response = connection.getresponse()
        if response.status != 206:
            raise OSError(f"the source answered a range with HTTP {response.status}")
        data = response.read(end - start + 2)
        if len(data) > end - start + 1:
            raise OSError("the source exceeded the requested range")
        return data
    finally:
        connection.close()
