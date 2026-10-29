"""The shelf a family finds on day one.

The image ships an approved-app list so that an ordinary family, a student
and a developer all find what they need without anybody curating anything
(the standing "complete out of the box" rule). These check it is coherent:
every entry is shelvable by the Store, the refs are real Flathub ids, and
the categories a family actually needs are represented.
"""

import json
import re
from pathlib import Path

import pytest

from kosherd import appkinds

ROOT = Path(__file__).parents[2]
CATALOG = ROOT / "os-image/files/etc/kosher/catalog.json"
APPS = json.loads(CATALOG.read_text())["apps"]


def test_every_entry_has_what_the_store_draws():
    for app in APPS:
        assert app["ref"] and app["name"] and app["summary"], app
        assert app.get("categories"), f"{app['ref']} has no categories to shelve it by"


def test_refs_are_flatpak_ids_nixpkgs_packages_or_known_builds_and_unique():
    from kosherd import upstreamapps

    refs = [a["ref"] for a in APPS]
    assert len(refs) == len(set(refs))
    for ref in refs:
        assert (re.fullmatch(r"[A-Za-z][\w-]*(\.[A-Za-z0-9][\w-]*){2,}", ref)
                or re.fullmatch(r"nixpkgs#(_?)[A-Za-z0-9][A-Za-z0-9_.-]*", ref)
                or ref[len(upstreamapps.PREFIX):] in upstreamapps.RELEASES), ref


def test_the_ai_tools_and_editors_come_from_nixpkgs():
    # Issue #34: most have no Flatpak. The shelf a developer finds.
    nix = {a["ref"] for a in APPS if a["ref"].startswith("nixpkgs#")}
    for package in ("claude-code", "codex", "opencode", "gemini-cli", "github-copilot-cli",
                    "github-copilot-app", "aider-chat", "gh", "code-cursor", "cursor-cli",
                    "antigravity", "antigravity-cli", "neovim"):
        assert f"nixpkgs#{package}" in nix, package
    # ...and every one of them sits on the Developer tools shelf, not on
    # Learning (ArtificialIntelligence is a Learning category).
    for app in APPS:
        if (app["ref"] in nix and "TerminalEmulator" not in app["categories"]
                and "Font" not in app["categories"]
                # A service, and a password manager, on Utilities (#213, #234).
                and app["ref"] not in ("nixpkgs#syncthing", "nixpkgs#_1password-gui",
                                       "nixpkgs#_1password-cli")):
            assert appkinds.kind_of(app) == "develop", app["ref"]


def test_the_terminals_run_on_the_host_and_sit_with_ptyxis():
    # A terminal in a Flatpak sandbox cannot see the host's PATH, the Nix
    # profile or podman, which is the whole point of a developer's
    # terminal; these come from nixpkgs, or from the developer's own build
    # where the Nix one cannot find the graphics drivers (kitty), and
    # shelve with Ptyxis.
    terminals = {a["ref"]: a for a in APPS if "TerminalEmulator" in a.get("categories", [])}
    for ref in ("upstream#kitty", "nixpkgs#ghostty", "nixpkgs#foot", "nixpkgs#alacritty",
                "nixpkgs#cool-retro-term"):
        assert ref in terminals, ref
        assert appkinds.kind_of(terminals[ref]) == appkinds.kind_of(terminals["app.devsuite.Ptyxis"])


def test_the_languages_from_the_issue_are_there_and_not_baked_into_the_image():
    # Issue #34: node (npm, bun, pnpm, yarn), python (uv, pip, pipenv,
    # poetry), rust, go, php, ruby, zig. From nixpkgs into the account's
    # profile, not into the image: pip comes with the system Python.
    nix = {a["ref"] for a in APPS if a["ref"].startswith("nixpkgs#")}
    for package in ("nodejs", "bun", "pnpm", "yarn", "python3", "uv", "pipenv", "poetry",
                    "rustup", "go", "php", "phpPackages.composer", "ruby", "zig"):
        assert f"nixpkgs#{package}" in nix, package
    containerfile = (ROOT / "os-image/Containerfile").read_text()
    for rpm in ("nodejs", "golang", "rust", "cargo", "php", "ruby", "zig"):
        assert not re.search(rf"dnf -y install[^\n]*\b{rpm}\b", containerfile), rpm


def test_nothing_falls_off_the_shelves():
    unshelved = [a["ref"] for a in APPS if appkinds.kind_of(a) == appkinds.OTHER]
    assert not unshelved, unshelved


def test_a_family_a_student_and_a_developer_all_find_something():
    counts: dict[str, int] = {}
    for app in APPS:
        kind = appkinds.kind_of(app)
        counts[kind] = counts.get(kind, 0) + 1
    for shelf, least in (("internet", 3), ("work", 4), ("pictures", 6),
                         ("music", 3), ("develop", 4), ("games", 3),
                         ("learning", 2), ("utilities", 4)):
        assert counts.get(shelf, 0) >= least, f"{shelf}: {counts.get(shelf, 0)}"


@pytest.mark.parametrize("ref", [
    "org.mozilla.firefox", "org.chromium.Chromium",          # browsers
    "org.videolan.VLC", "org.kde.kdenlive", "io.bassi.Amberol",  # media
    "org.libreoffice.LibreOffice", "org.mozilla.Thunderbird",     # productivity
    "com.visualstudio.code", "org.gnome.Builder", "dev.zed.Zed",  # development
])
def test_the_apps_people_ask_for_by_name_are_there(ref):
    assert any(a["ref"] == ref for a in APPS)


def test_the_catalog_says_what_an_approval_means():
    comment = json.loads(CATALOG.read_text())["comment"]
    assert "Approved apps only" in comment
    assert "whole store" in comment


def test_the_store_and_the_shipped_list_use_one_vocabulary():
    # The Store's shelves ARE appkinds.KINDS; a second copy in the Store's
    # source is what this file used to regex-parse, and what drifted.
    store = (ROOT / "store-app/src/kosherstore/app.py").read_text()
    assert "from kosherd import appkinds" in store or "kosherd.appkinds" in store
    assert 'SHELVES = (' not in store


def test_fonts_have_their_own_shelf_and_no_app_launchers():
    fonts = [a for a in APPS if appkinds.kind_of(a) == "fonts"]
    refs = {a["ref"] for a in fonts}
    assert {"nixpkgs#nerd-fonts.jetbrains-mono", "nixpkgs#nerd-fonts.fira-code",
            "nixpkgs#nerd-fonts.hack", "nixpkgs#noto-fonts", "nixpkgs#inter",
            "nixpkgs#liberation_ttf", "nixpkgs#noto-fonts-color-emoji"} <= refs
    assert all("exec" not in font for font in fonts)
    assert len(refs) == len(fonts)


# -- the approved ids are on Flathub (#184) -----------------------------------
#
# Flathub renames apps (org.gnome.Endeavour became org.gnome.Todo,
# org.gnome.Clocks became org.gnome.clocks), retire them (org.gnome.Dictionary
# is end-of-life and gone from the listing) and some never get there (GNOME
# Console). An approved id Flathub does not have is an app a family picks
# and cannot install, and nothing else notices until somebody tries. So ask
# Flathub, when it can be asked: offline, or with OFFLINE=1, this skips;
# online, a 404 fails.

FLATHUB_APP = "https://flathub.org/api/v2/appstream/{}"


def _flathub_status(ref: str) -> int | None:
    """Flathub's HTTP status for this app id, None when it cannot be reached."""
    import urllib.error
    import urllib.request

    request = urllib.request.Request(FLATHUB_APP.format(ref), method="HEAD",
                                     headers={"User-Agent": "KosherOS catalog test"})
    try:
        with urllib.request.urlopen(request, timeout=10) as answer:
            return answer.status
    except urllib.error.HTTPError as error:
        return error.code
    except (urllib.error.URLError, OSError):
        return None


def test_every_approved_flatpak_is_on_flathub():
    import os
    from concurrent.futures import ThreadPoolExecutor

    if os.environ.get("OFFLINE"):
        pytest.skip("OFFLINE is set")
    # Flathub itself has to answer first, or a 404 cannot be told from a
    # network that is down or behind a portal.
    if _flathub_status("org.mozilla.firefox") != 200:
        pytest.skip("Flathub cannot be reached from here")
    refs = [a["ref"] for a in APPS if "#" not in a["ref"]]
    with ThreadPoolExecutor(max_workers=8) as pool:
        status = dict(zip(refs, pool.map(_flathub_status, refs)))
    missing = sorted(ref for ref, code in status.items() if code == 404)
    assert not missing, f"not on Flathub: {missing}"
