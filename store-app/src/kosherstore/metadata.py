"""Fetch details for one selected app, as the current user, without installing it."""

from __future__ import annotations

import json
import os
import re
import subprocess
from html.parser import HTMLParser
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen

from kosherd import nixapps


class DetailsError(Exception):
    pass


def web_url(value) -> str:
    if not isinstance(value, str):
        return ""
    try:
        parsed = urlsplit(value.strip())
        return value.strip() if parsed.scheme in ("http", "https") and parsed.hostname else ""
    except ValueError:
        return ""


def fetch_bytes(url: str, limit: int = 8 * 1024 * 1024) -> bytes:
    if not web_url(url):
        raise DetailsError("This link is not a web address.")
    request = Request(url, headers={"User-Agent": "KosherOS-Store/1.0"})
    with urlopen(request, timeout=15) as response:
        if not web_url(response.url):
            raise DetailsError("The download redirected to an unsupported address.")
        data = response.read(limit + 1)
    if len(data) > limit:
        raise DetailsError("The download is too large to display.")
    return data


class _Description(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.hidden += 1
        elif not self.hidden:
            if tag in ("p", "ul", "ol", "div"):
                self.parts.append("\n\n")
            elif tag == "li":
                self.parts.append("\n• ")
            elif tag == "br":
                self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.hidden = max(0, self.hidden - 1)
        elif not self.hidden and tag in ("p", "ul", "ol", "div"):
            self.parts.append("\n\n")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(re.sub(r"\s+", " ", data))


def plain_description(value) -> str:
    parser = _Description()
    parser.feed(str(value or ""))
    text = "".join(parser.parts)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(line.strip() for line in text.splitlines())).strip()


def flatpak_details(ref: str) -> dict:
    if not re.fullmatch(r"[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)+", ref):
        raise DetailsError("This app does not have a valid Flathub ID.")
    raw = json.loads(fetch_bytes("https://flathub.org/api/v2/appstream/" + quote(ref, safe=""),
                                 limit=2 * 1024 * 1024))
    if not isinstance(raw, dict) or raw.get("id") != ref:
        raise DetailsError("Flathub did not return details for this app.")
    screenshots = []
    shots = sorted(raw.get("screenshots") or [], key=lambda shot: not shot.get("default"))
    for shot in shots[:8]:
        sizes = shot.get("sizes") or []
        image = next((size for size in sizes if web_url(size.get("src"))), None)
        if image:
            screenshots.append({"url": web_url(image["src"]), "caption": shot.get("caption") or ""})
    releases = [r for r in raw.get("releases") or [] if r.get("type") in (None, "stable")]
    return {
        "description": plain_description(raw.get("description")),
        "screenshots": screenshots,
        "developer": raw.get("developer_name") or "",
        "license": raw.get("project_license") or "",
        "homepage": web_url((raw.get("urls") or {}).get("homepage")),
        "version": releases[0].get("version", "") if releases else "",
    }


# Evaluate only the chosen attribute and a small, serializable set of fields.
# Never evaluate the entire package set or build/install a derivation.
NIX_DETAILS = """p: {
  version = p.version or "";
  description = p.meta.description or "";
  longDescription = p.meta.longDescription or "";
  homepage = p.meta.homepage or "";
  license = p.meta.license or [];
  maintainers = map (m: { name = m.name or ""; github = m.github or ""; })
    (p.meta.maintainers or []);
  mainProgram = p.meta.mainProgram or "";
}"""


def nix_details(ref: str) -> dict:
    name = nixapps.package(ref)
    try:
        result = subprocess.run(
            ["nix", "eval", "--json", "--impure", nixapps.PREFIX + name,
             "--apply", NIX_DETAILS],
            env={**os.environ, "NIXPKGS_ALLOW_UNFREE": "1"},
            capture_output=True, text=True, timeout=90)
    except FileNotFoundError as error:
        raise DetailsError("Nix is not installed on this computer.") from error
    except subprocess.TimeoutExpired as error:
        raise DetailsError("The package lookup timed out. Try again.") from error
    if result.returncode:
        raise DetailsError("Could not read this package from nixpkgs. Check your connection and try again.")
    raw = json.loads(result.stdout)
    licenses = raw.get("license") or []
    if not isinstance(licenses, list):
        licenses = [licenses]
    license_names = [item.get("spdxId") or item.get("fullName") or item.get("shortName") or ""
                     if isinstance(item, dict) else str(item) for item in licenses]
    homepage = raw.get("homepage") or ""
    if isinstance(homepage, list):
        homepage = next((url for url in homepage if web_url(url)), "")
    return {
        "description": (raw.get("longDescription") or raw.get("description") or "").strip(),
        "version": raw.get("version") or "",
        "license": ", ".join(name for name in license_names if name),
        "homepage": web_url(homepage),
        "maintainers": ", ".join(m.get("name") or m.get("github") or ""
                                 for m in raw.get("maintainers") or []).strip(", "),
        "main_program": raw.get("mainProgram") or "",
        "screenshots": [],
    }


def load_details(app: dict) -> dict:
    if nixapps.is_nix(app["ref"]):
        return nix_details(app["ref"])
    if str(app["ref"]).startswith("upstream#"):
        # Everything there is to say is in the catalog entry: asking the
        # developer's site would only add a wait to opening the page.
        return {"description": app.get("description") or app.get("summary") or "",
                "developer": app.get("developer") or "",
                "license": app.get("license") or "",
                "homepage": web_url(app.get("homepage") or ""),
                "screenshots": []}
    if app.get("source") == "image":
        return {"description": app.get("description") or app.get("summary") or "",
                "screenshots": app.get("screenshots") or []}
    return flatpak_details(app["ref"])
