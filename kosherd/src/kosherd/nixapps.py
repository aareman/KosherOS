"""Apps from nixpkgs: the second kind of app the Store carries.

Most AI coding tools and a few editors (issue #34) have no Flatpak, and
KosherOS ships no package repository of its own. Nix is on every machine,
and nixpkgs carries them all — so a catalog entry whose ref is
`nixpkgs#<package>` is installed from upstream nixpkgs the way a Flatpak
entry is installed from upstream Flathub: only by kosherd, only for an
account appaccess says may have it.

Where a Flatpak is installed system-wide, a Nix app goes into the asking
account's OWN profile (~/.local/state/nix/profiles/profile), run as that
account. That is what makes the approval per account without a second
run-time gate: nobody else's PATH sees it, and the Nix daemon itself
refuses an account whose kind of internet is "No internet"
(nixdaemon.py). The account's side of the install — evaluating nixpkgs,
fetching its tarball — goes through that account's filter like anything
else it opens; the build's fetches run as the build users, which the
firewall holds to the registries.

Unfree packages (Claude Code, Cursor, Copilot) are allowed: the licence
is the publisher's to grant and the person installing is the one who
agrees to it, which is also what Flathub's non-free apps come to.
"""

from __future__ import annotations

import json
import logging
import os
import pwd
import shutil
import subprocess
from collections import deque
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)

PREFIX = "nixpkgs#"
# The bundle the account's nix client trusts (kosher-ca.sh sets the same).
CA_BUNDLE = "/etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem"


class NixAppError(Exception):
    pass


def is_nix(ref: str) -> bool:
    return isinstance(ref, str) and ref.startswith(PREFIX)


def package(ref: str) -> str:
    """'nixpkgs#claude-code' -> 'claude-code'. Refuses anything that is not
    a plain attribute name: a ref is also an argument to nix."""
    name = ref[len(PREFIX):] if is_nix(ref) else ""
    # A leading underscore is how nixpkgs names a package that starts with a
    # digit (_1password-gui); a leading dash would be an option to nix.
    if (not name or not (name[0].isalnum() or (name[0] == "_" and name[1:2].isalnum()))
            or not all(c.isalnum() or c in "-_." for c in name)):
        raise NixAppError(f"{ref!r} is not a nixpkgs package reference")
    return name


def profile_dir(uid: int) -> Path:
    """Where nix keeps this account's profile (the current generation)."""
    return Path(pwd.getpwuid(uid).pw_dir) / ".local/state/nix/profiles/profile"


def _profile_entries(uid: int) -> dict[str, str]:
    """Map installed refs to Nix profile element names for upgrade/removal.

    No nix invocation: the manifest is a small JSON file, and this is
    asked on every Store open. Version 3 keys the elements by package
    name; older manifests list them, named by their attribute path.
    """
    try:
        doc = json.loads((profile_dir(uid) / "manifest.json").read_text())
    except (KeyError, OSError, ValueError):
        return {}
    elements = doc.get("elements")
    found: dict[str, str] = {}
    if isinstance(elements, dict):
        items = elements.items()
    elif isinstance(elements, list):
        items = ((str(i), e) for i, e in enumerate(elements) if isinstance(e, dict))
    else:
        return found
    for name, element in items:
        if name and isinstance(element, dict) and element.get("active", True):
            attribute = element.get("attrPath") or name
            if attribute.startswith(("legacyPackages.", "packages.")):
                attribute = attribute.split(".", 2)[-1]
            found[PREFIX + attribute] = name
    return found


def installed(uid: int) -> set[str]:
    return set(_profile_entries(uid))


def _make_dirs_as(path: Path, pw) -> None:
    """Create `path` and any missing parents inside the account's home,
    each owned by the account.

    The Store's root install runs before an account may ever have signed
    in, when ~/.local does not exist yet. Made by root, ~/.local was
    root's: the account could not write ~/.local/share, so the app got no
    launcher and the desktop's own first sign-in had nowhere to keep its
    files. Only what nix itself writes inside the profiles directory (the
    generation links) is root's.
    """
    home = Path(pw.pw_dir)
    missing = []
    current = path
    while current != home and home in current.parents and not current.exists():
        missing.append(current)
        current = current.parent
    for directory in reversed(missing):
        directory.mkdir()
        shutil.chown(directory, pw.pw_uid, pw.pw_gid)


def _link_profile_as(profile: Path, pw) -> None:
    """~/.nix-profile -> the profile, owned by the account, as nix makes it
    when it runs as the account itself.

    The session's PATH finds Store apps through ~/.nix-profile/bin
    (environment.d). `nix --profile` as root writes the profile and makes
    no such link, so an app installed that way was not on the PATH, and
    the desktop hid its launcher as a command it could not find. An
    existing link or file is the account's and is left alone.
    """
    link = Path(pw.pw_dir) / ".nix-profile"
    if link.exists() or link.is_symlink():
        return
    link.symlink_to(profile)
    os.lchown(link, pw.pw_uid, pw.pw_gid)


def _argv(uid: int, *nix_args: str, as_root: bool = False, profile: bool = True) -> list[str]:
    """`nix ...` as the account, with a clean environment of its own — or,
    for an account without developer tools (#123), as root into that
    account's profile: the daemon refuses such an account directly, and
    the Store is its only way to install.

    runuser alone would hand nix root's HOME, and the profile would land
    in /root. --impure is what lets NIXPKGS_ALLOW_UNFREE through. A
    command that only evaluates (`nix eval`) takes no --profile.
    """
    pw = pwd.getpwuid(uid)
    if as_root:
        argv = ["env", "NIXPKGS_ALLOW_UNFREE=1", f"NIX_SSL_CERT_FILE={CA_BUNDLE}",
                "nix", *nix_args]
        if profile:
            target = profile_dir(uid)
            _make_dirs_as(target.parent, pw)
            _link_profile_as(target, pw)
            argv += ["--profile", str(target)]
        return argv
    return [
        "runuser", "-u", pw.pw_name, "--",
        "env", "-i",
        f"HOME={pw.pw_dir}", f"USER={pw.pw_name}", f"LOGNAME={pw.pw_name}",
        "PATH=/usr/bin:/bin",
        "NIXPKGS_ALLOW_UNFREE=1",
        f"NIX_SSL_CERT_FILE={CA_BUNDLE}",
        "nix", *nix_args,
    ]


def _status(line: str) -> str:
    """What nix's log line means, in the words the Store's progress shows."""
    text = line.lower()
    if text.startswith(("copying path", "downloading", "unpacking", "fetching")):
        return "Downloading…"
    if text.startswith("building"):
        return "Building…"
    if "evaluating" in text or text.startswith("warning:"):
        return "Looking up…"
    return "Working…"


def _run(ref: str, argv: list[str], on_progress: Callable[[str, int, str], None]) -> None:
    proc = subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                            text=True)
    tail: deque[str] = deque(maxlen=20)
    assert proc.stderr is not None
    for raw in proc.stderr:
        line = raw.strip()
        if not line:
            continue
        tail.append(line)
        on_progress(ref, 0, _status(line))
    code = proc.wait()
    if code != 0:
        # The last line nix printed that is not a progress line is the
        # reason: "not allowed to connect", a hash mismatch, no network.
        reason = next((l for l in reversed(tail) if l.startswith("error")), None) \
            or (tail[-1] if tail else f"nix exited {code}")
        raise NixAppError(reason.removeprefix("error:").strip())


def _desktop(uid: int) -> None:
    """Link the profile's desktop files and icons into the account's own
    ~/.local/share (nixdesktop.py), as the account. Never a failure: an
    app that installed but has no desktop entry yet is still installed."""
    argv = _argv(uid)
    argv[argv.index("nix"):] = ["kosher-nix-desktop", "-q"]
    res = subprocess.run(argv, capture_output=True, text=True)
    if res.returncode != 0:
        log.warning("desktop entries for uid %d not linked: %s", uid, (res.stderr or "").strip())


def install(ref: str, uid: int, on_progress, as_root: bool = False) -> None:
    name = package(ref)
    on_progress(ref, 0, "Looking up…")
    _run(ref, _argv(uid, "profile", "install", "--impure", PREFIX + name, as_root=as_root), on_progress)
    _desktop(uid)


def remove(ref: str, uid: int, on_progress, as_root: bool = False) -> None:
    name = package(ref)
    name = _profile_entries(uid).get(ref, name)
    on_progress(ref, 0, "Removing…")
    _run(ref, _argv(uid, "profile", "remove", name, as_root=as_root), on_progress)
    _desktop(uid)


def upgrade(ref: str, uid: int, on_progress, as_root: bool = False) -> None:
    name = package(ref)
    name = _profile_entries(uid).get(ref, name)
    on_progress(ref, 0, "Updating…")
    _run(ref, _argv(uid, "profile", "upgrade", "--impure", name, as_root=as_root), on_progress)
    _desktop(uid)


# -- what has a newer build ----------------------------------------------------

def _profile_paths(manifest: dict) -> dict[str, tuple[str, list[str]]]:
    """{ref: (element name, store paths)} for the nixpkgs packages in a
    profile manifest that `nix profile upgrade` can move: the ones
    installed from an unlocked `nixpkgs#…` reference. A package pinned to
    one revision has no "latest" and is left out."""
    elements = manifest.get("elements")
    if isinstance(elements, dict):
        items = elements.items()
    elif isinstance(elements, list):
        items = ((str(i), e) for i, e in enumerate(elements) if isinstance(e, dict))
    else:
        return {}
    found = {}
    for name, element in items:
        if not (name and isinstance(element, dict) and element.get("active", True)):
            continue
        if element.get("originalUrl") not in ("flake:nixpkgs", "nixpkgs"):
            continue
        attribute = element.get("attrPath") or name
        if attribute.startswith(("legacyPackages.", "packages.")):
            attribute = attribute.split(".", 2)[-1]
        found[PREFIX + attribute] = (name, list(element.get("storePaths") or []))
    return found


def _latest_expr(attributes: list[str]) -> str:
    """One nix expression for every package at once: its current store
    path and version in the channel the registry points at. A package
    nixpkgs no longer has, or cannot evaluate, is null."""
    names = " ".join(json.dumps(a) for a in attributes)
    return (
        'let flake = builtins.getFlake "nixpkgs"; '
        'pkgs = flake.legacyPackages.${builtins.currentSystem}; lib = flake.lib; '
        'look = attr: let d = lib.attrByPath (lib.splitString "." attr) null pkgs; '
        'r = builtins.tryEval (if d == null then null else '
        '{ out = d.outPath; version = d.version or ""; pname = d.pname or d.name; }); '
        'in if r.success then r.value else null; '
        f'in builtins.listToAttrs (map (a: {{ name = a; value = look a; }}) [ {names} ])'
    )


def latest(attributes: list[str], nix: list[str]) -> dict[str, dict | None]:
    """{attribute: {out, version, pname} | None} from the channel, with `nix`
    the command prefix to run (`_argv(uid, as_root=…, profile=False)` in the
    daemon, a plain `nix` for a person's own shell). Goes to the network for
    the channel's news, which is why a check asks for it and a window
    opening does not."""
    if not attributes:
        return {}
    res = subprocess.run([*nix, "eval", "--impure", "--json", "--expr", _latest_expr(attributes)],
                         capture_output=True, text=True, timeout=600)
    if res.returncode != 0:
        reason = next((l for l in reversed(res.stderr.splitlines()) if l.startswith("error")),
                      res.stderr.strip() or f"nix exited {res.returncode}")
        raise NixAppError(reason.removeprefix("error:").strip())
    return json.loads(res.stdout or "{}")


def _main_output(paths: list[str]) -> str:
    """The package's own path among its outputs: '…-tmux-3.7c' rather than
    '…-tmux-3.7c-man', which is the shortest name."""
    return min(paths, key=lambda p: (len(p.rsplit("/", 1)[-1]), p))


def _installed_version(store_path: str, pname: str) -> str:
    """'…-opencode-2.1.4' with pname 'opencode' -> '2.1.4'."""
    name = store_path.rsplit("/", 1)[-1]
    name = name[33:] if len(name) > 33 and name[32] == "-" else name
    return name[len(pname) + 1:] if pname and name.startswith(pname + "-") else name


def _short(store_path: str) -> str:
    return store_path.rsplit("/", 1)[-1][:12]


def compare(profile: dict[str, tuple[str, list[str]]], channel: dict[str, dict | None],
            names: dict[str, str] | None = None) -> list[dict]:
    """Which installed packages the channel has a different build of:
    [{ref, name, version, commit, latest}], the shape the Store's Updates
    shelf reads for Flatpaks. `version` and `commit` are what is
    installed; `latest` is what an upgrade would bring — the new version,
    or, when the channel rebuilt the same version (a dependency moved),
    the new build's hash, as a Flatpak's `latest` is its new commit."""
    found = []
    for ref, (_element, paths) in profile.items():
        current = channel.get(ref[len(PREFIX):])
        if not current or not paths or current["out"] in paths:
            continue
        main = _main_output(paths)
        installed = _installed_version(main, current.get("pname") or "")
        newer = current.get("version") or ""
        found.append({
            "ref": ref,
            "name": (names or {}).get(ref) or package(ref),
            "version": installed,
            "commit": _short(main),
            "latest": newer if newer and newer != installed else _short(current["out"]),
        })
    return sorted(found, key=lambda e: e["name"].lower())


def updates(uid: int, refs: set[str], names: dict[str, str] | None = None,
            as_root: bool = False) -> list[dict]:
    """The Store's nixpkgs apps in this account's profile that the channel
    has a newer build of. `refs` says which of the profile's packages are
    the Store's (the rest are the developer's own, `kosher --local`)."""
    try:
        manifest = json.loads((profile_dir(uid) / "manifest.json").read_text())
    except (OSError, ValueError):
        return []
    profile = {ref: entry for ref, entry in _profile_paths(manifest).items() if ref in refs}
    if not profile:
        return []
    attributes = [ref[len(PREFIX):] for ref in profile]
    channel = latest(attributes, _argv(uid, as_root=as_root, profile=False))
    return compare(profile, channel, names)


def own_updates() -> list[dict]:
    """For a person's own shell (`kosher update --local`): every unlocked
    nixpkgs package in their profile with a newer build, the Store's and
    their own alike. Runs nix as whoever calls."""
    try:
        manifest = json.loads(Path.home().joinpath(
            ".local/state/nix/profiles/profile/manifest.json").read_text())
    except (OSError, ValueError):
        return []
    profile = _profile_paths(manifest)
    if not profile:
        return []
    nix = ["env", "NIXPKGS_ALLOW_UNFREE=1", f"NIX_SSL_CERT_FILE={CA_BUNDLE}", "nix"]
    return compare(profile, latest([ref[len(PREFIX):] for ref in profile], nix))


def own_upgrade(ref: str) -> int:
    """`nix profile upgrade` of one of the person's own packages, in their
    shell, output and all. The exit code is nix's."""
    try:
        manifest = json.loads(Path.home().joinpath(
            ".local/state/nix/profiles/profile/manifest.json").read_text())
    except (OSError, ValueError):
        manifest = {}
    element = _profile_paths(manifest).get(ref, (package(ref), []))[0]
    return subprocess.call(["env", "NIXPKGS_ALLOW_UNFREE=1", f"NIX_SSL_CERT_FILE={CA_BUNDLE}",
                            "nix", "profile", "upgrade", "--impure", element])


def exists(ref: str) -> bool | None:
    """Whether nixpkgs has this package: for `kosherctl check-catalog`.
    None where nix is not on this machine (nothing can be said)."""
    if shutil.which("nix") is None:
        return None
    name = package(ref)
    res = subprocess.run(
        ["env", "NIXPKGS_ALLOW_UNFREE=1", "nix", "eval", "--impure", "--raw",
         f"{PREFIX}{name}.name"],
        capture_output=True, text=True, timeout=600)
    return res.returncode == 0
