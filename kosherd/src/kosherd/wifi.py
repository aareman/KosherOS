"""Wi-Fi for first boot, through NetworkManager's nmcli.

The setup wizard runs before any account exists, as root inside a kiosk
compositor, on a machine that may have nothing but a Wi-Fi card between it
and the update server. So the wizard offers to join a network — and it
asks kosherd to do it, the way it asks kosherd for everything else, so the
text wizard and any future caller get the same three calls.

Why nmcli rather than NetworkManager's D-Bus API: the whole job is "list
networks, join one", nmcli's terse output was made to be parsed, and the
parsing lives in pure functions here that the tests can feed transcripts.
"""

from __future__ import annotations

import subprocess

TIMEOUT_LIST = 30     # a rescan on a slow card
TIMEOUT_CONNECT = 90  # DHCP on a bad day


class WifiError(Exception):
    """Something nmcli refused, in words a parent can act on."""


def split_terse(line: str) -> list[str]:
    """Split one line of `nmcli -t` output on its unescaped colons.

    Terse mode separates fields with ':' and escapes a ':' inside a value
    as '\\:' — an SSID may contain one, and a MAC address always does.
    """
    fields, current, escaped = [], [], False
    for ch in line:
        if escaped:
            current.append(ch)
            escaped = False
        elif ch == "\\":
            escaped = True
        elif ch == ":":
            fields.append("".join(current))
            current = []
        else:
            current.append(ch)
    fields.append("".join(current))
    return fields


# -- what the machine is connected to ------------------------------------------

def parse_devices(text: str) -> dict:
    """`nmcli -t -f TYPE,STATE,CONNECTION device` -> the status the wizard
    shows. Wired beats wireless in the answer, because it is the surer
    connection and the one a person would not think to mention."""
    online_wired = online_wifi = None
    wifi_hardware = False
    for line in text.splitlines():
        fields = split_terse(line.strip())
        if len(fields) < 3:
            continue
        kind, state, name = fields[0], fields[1], fields[2]
        if kind == "wifi":
            wifi_hardware = True
        if not state.startswith("connected"):
            continue
        if kind == "ethernet" and online_wired is None:
            online_wired = name
        elif kind == "wifi" and online_wifi is None:
            online_wifi = name
    if online_wired is not None:
        return {"online": True, "kind": "ethernet", "name": online_wired,
                "wifi_hardware": wifi_hardware}
    if online_wifi is not None:
        return {"online": True, "kind": "wifi", "name": online_wifi,
                "wifi_hardware": wifi_hardware}
    return {"online": False, "kind": "", "name": "", "wifi_hardware": wifi_hardware}


def status() -> dict:
    try:
        res = subprocess.run(["nmcli", "-t", "-f", "TYPE,STATE,CONNECTION", "device"],
                             capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        # No NetworkManager at all: nothing to offer, but not a fault the
        # wizard should stop for.
        return {"online": False, "kind": "", "name": "", "wifi_hardware": False}
    return parse_devices(res.stdout if res.returncode == 0 else "")


# -- what is in the air --------------------------------------------------------

def parse_networks(text: str) -> list[dict]:
    """`nmcli -t -f SSID,SIGNAL,SECURITY,IN-USE dev wifi list` -> one entry
    per network name, strongest first, the one in use at the very top.

    A router with several access points shows up once per radio; a person
    knows it by one name, so the strongest is kept and the rest dropped.
    Hidden networks (an empty SSID) are dropped too: there is nothing to
    show and no way to pick them from a list.
    """
    best: dict[str, dict] = {}
    for line in text.splitlines():
        fields = split_terse(line.strip())
        if len(fields) < 4:
            continue
        ssid, signal, security, in_use = fields[0], fields[1], fields[2], fields[3]
        if not ssid:
            continue
        try:
            strength = int(signal)
        except ValueError:
            strength = 0
        entry = {"ssid": ssid, "signal": strength,
                 "secured": security.strip() not in ("", "--"),
                 "active": in_use.strip() == "*"}
        seen = best.get(ssid)
        if seen is None or entry["active"] or (not seen["active"]
                                                and strength > seen["signal"]):
            best[ssid] = entry
    return sorted(best.values(), key=lambda e: (not e["active"], -e["signal"], e["ssid"]))


def networks() -> list[dict]:
    try:
        res = subprocess.run(["nmcli", "-t", "-f", "SSID,SIGNAL,SECURITY,IN-USE",
                              "device", "wifi", "list", "--rescan", "yes"],
                             capture_output=True, text=True, timeout=TIMEOUT_LIST)
    except FileNotFoundError:
        raise WifiError("This computer has no network manager to scan with.")
    except subprocess.TimeoutExpired:
        raise WifiError("Scanning for networks took too long. Try again.")
    if res.returncode != 0:
        raise WifiError(friendly(res.stderr or res.stdout))
    return parse_networks(res.stdout)


# -- joining one ---------------------------------------------------------------

def friendly(message: str) -> str:
    """nmcli's error, said the way the person at the wizard needs it."""
    text = message.strip()
    lowered = text.lower()
    if "secrets were required" in lowered or "802-11-wireless-security" in lowered \
            or "no key" in lowered or "password" in lowered and "invalid" in lowered:
        return "That password was not accepted. Check it and try again."
    if "could not be found" in lowered or "no network with ssid" in lowered:
        return "That network is out of range now. Pick another or try again."
    if "timeout" in lowered or "timed out" in lowered:
        return "Connecting took too long. Try again, or move closer to the router."
    if "not authorized" in lowered or "not permitted" in lowered:
        return "The computer would not allow the connection. Setup can continue without it."
    text = text.removeprefix("Error:").strip()
    return text or "Could not connect."


def connect(ssid: str, password: str = "") -> None:
    if not ssid.strip():
        raise WifiError("Pick a network first.")
    argv = ["nmcli", "device", "wifi", "connect", ssid]
    if password:
        argv += ["password", password]
    try:
        res = subprocess.run(argv, capture_output=True, text=True, timeout=TIMEOUT_CONNECT)
    except FileNotFoundError:
        raise WifiError("This computer has no network manager to connect with.")
    except subprocess.TimeoutExpired:
        raise WifiError(friendly("timeout"))
    if res.returncode != 0:
        raise WifiError(friendly(res.stderr or res.stdout))
