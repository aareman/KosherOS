"""The time zone question at first boot, for the graphical wizard and the
text setup alike.

The installer answers the time zone with the kickstart's fixed default and
does not ask, so the first-boot wizard does: it guesses from the
internet, or shows what the machine has now, and the person confirms or
picks another. Local time decides when screen-time schedules start and end
and where one day of usage stops, so a family in Jerusalem on New York
time gets its limits seven hours off.

Both wizards run as root (kosher-firstboot.service has no User=), and
timedated lets root change the zone without asking polkit, so this talks to
org.freedesktop.timedate1 directly rather than through kosherd.
"""

from __future__ import annotations

import json
import os
import urllib.request
from dataclasses import dataclass
from pathlib import Path

ZONEINFO = Path("/usr/share/zoneinfo")
# The service Anaconda uses for the same guess. It answers with the
# caller's approximate location, including "time_zone".
GEOIP_URL = "https://geoip.fedoraproject.org/city"
GEOIP_TIMEOUT = 4.0


@dataclass(frozen=True)
class Zone:
    id: str          # "America/New_York"
    city: str        # "New York"
    country: str     # "United States", or "" when the zone has none

    @property
    def label(self) -> str:
        """How a person reads it: "New York, United States"."""
        return f"{self.city}, {self.country}" if self.country else self.city

    def matches(self, text: str) -> bool:
        words = text.casefold().replace("_", " ").split()
        hay = f"{self.id.replace('_', ' ')} {self.city} {self.country}".casefold()
        return all(w in hay for w in words)


def _city(zone_id: str) -> str:
    return zone_id.rsplit("/", 1)[-1].replace("_", " ")


def known_ids(root: Path = ZONEINFO) -> set[str]:
    """Every name timedated accepts, as `timedatectl list-timezones` lists
    them: the zones and links in tzdata.zi, or the zone tables when it is
    missing."""
    ids: set[str] = {"UTC"}
    try:
        for line in (root / "tzdata.zi").read_text().splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[0] == "Z":
                ids.add(parts[1])
            elif len(parts) >= 3 and parts[0] == "L":
                ids.add(parts[2])
    except OSError:
        pass
    for table in ("zone1970.tab", "zone.tab"):
        for row in _rows(root / table):
            if len(row) >= 3:
                ids.add(row[2])
    return ids


def _rows(path: Path) -> list[list[str]]:
    try:
        text = path.read_text()
    except OSError:
        return []
    return [line.split("\t") for line in text.splitlines()
            if line and not line.startswith("#")]


def zones(root: Path = ZONEINFO) -> list[Zone]:
    """The places to choose from: one row per zone in zone.tab, which is
    by country, so Jerusalem says Israel and Oslo is there by its own
    name. Sorted by city. UTC is last, for a machine that wants it."""
    countries = {row[0]: row[1] for row in _rows(root / "iso3166.tab") if len(row) >= 2}
    seen: dict[str, Zone] = {}
    for row in _rows(root / "zone.tab"):
        if len(row) >= 3 and row[2] not in seen:
            code = row[0].split(",")[0]
            seen[row[2]] = Zone(row[2], _city(row[2]), countries.get(code, ""))
    listed = sorted(seen.values(), key=lambda z: (z.city.casefold(), z.country))
    return listed + [Zone("UTC", "UTC", "")]


def zone_for(zone_id: str, listed: list[Zone]) -> Zone:
    """The row for an id, or one made up for a valid name the list lacks
    (a link such as "Asia/Calcutta")."""
    for z in listed:
        if z.id == zone_id:
            return z
    return Zone(zone_id, _city(zone_id), "")


def valid(zone_id: str, root: Path = ZONEINFO) -> bool:
    return bool(zone_id) and zone_id in known_ids(root)


def current(root: Path = ZONEINFO) -> str:
    """The zone the machine has now: the installer's answer, on first boot."""
    try:
        target = os.readlink("/etc/localtime")
    except OSError:
        return "UTC"
    marker = "zoneinfo/"
    zone_id = target.split(marker, 1)[1] if marker in target else ""
    return zone_id if valid(zone_id, root) else "UTC"


def guess(timeout: float = GEOIP_TIMEOUT, root: Path = ZONEINFO) -> str:
    """The zone the internet thinks this machine is in, or "" when there is
    no answer, no network, or an answer this system does not know."""
    try:
        request = urllib.request.Request(GEOIP_URL, headers={"Accept": "application/json"})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            answer = json.loads(response.read(65536).decode("utf-8", "replace"))
    except Exception:  # noqa: BLE001 - offline, slow, refused: no guess
        return ""
    zone_id = answer.get("time_zone") if isinstance(answer, dict) else None
    return zone_id if isinstance(zone_id, str) and valid(zone_id, root) else ""


def resolve(text: str, root: Path = ZONEINFO) -> str:
    """What the text setup's answer names: a zone id in any case, or a city
    that only one zone has ("jerusalem", "new york"). "" when it is
    neither."""
    text = text.strip()
    if not text:
        return ""
    ids = known_ids(root)
    by_fold = {i.casefold(): i for i in ids}
    if text.casefold() in by_fold:
        return by_fold[text.casefold()]
    wanted = text.casefold().replace("_", " ")
    hits = [z.id for z in zones(root) if z.city.casefold() == wanted]
    return hits[0] if len(hits) == 1 else ""


def set_zone(zone_id: str) -> None:
    """Set the machine's time zone and turn network time on.

    Raises ValueError for a name the system does not know, and the D-Bus
    error when timedated refuses. Network time is best effort: a machine
    without an NTP service still gets its zone.
    """
    if not valid(zone_id):
        raise ValueError(f"{zone_id!r} is not a time zone this computer knows")
    import gi

    gi.require_version("Gio", "2.0")
    from gi.repository import Gio, GLib

    bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)

    def call(method: str, value) -> None:
        # interactive=False: as root this needs no polkit; anyone else is
        # refused outright rather than shown a password dialog.
        bus.call_sync("org.freedesktop.timedate1", "/org/freedesktop/timedate1",
                      "org.freedesktop.timedate1", method,
                      GLib.Variant("(sb)" if method == "SetTimezone" else "(bb)",
                                   (value, False)),
                      None, Gio.DBusCallFlags.NONE, 30000, None)

    call("SetTimezone", zone_id)
    try:
        call("SetNTP", True)
    except GLib.Error:
        pass
