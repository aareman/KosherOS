"""Syncthing, from the Store, as a service an account runs (#213).

Syncthing keeps folders the same across a person's own devices. It is not
a window but a background service with a web page on 127.0.0.1, and its
traffic cannot be read: devices talk BEP over TLS with pinned device
certificates on TCP 22000 (or QUIC on UDP 22000, or through a relay on
22067), and find each other through global discovery (HTTPS to
discovery.syncthing.net, pinned by its device ID as well) and local
discovery (UDP 21027 on the LAN). Through the proxy it would simply fail;
let out directly, the files it brings in arrive unfiltered. So a
supervised account has it only with an administrator's approval for that
account (appaccess.INDIVIDUAL_APPROVAL).

The rule the firewall keeps is narrower than "this account may use port
22000": the ONLY thing that may is Syncthing. Matching the account's uid
would let anything the account runs use those ports, so instead:

- Syncthing runs as a SYSTEM service per account, syncthing@<user>.service
  (the template in /usr/lib/systemd/system), with User=<user>. kosherd
  starts and stops it; the account cannot override a system unit, and a
  copy of Syncthing it starts itself runs in its own session's cgroup.
- The unit's ExecStart is kosherd's drop-in, naming the Nix store path of
  a Syncthing that kosherd built itself, as root, from nixpkgs — not the
  account's profile, which a developer account could point anywhere.
  The store is read-only, and the unit may execute nothing else
  (NoExecPaths=/), so Syncthing's "external versioning" cannot start a
  program inside the service. PrivateUsers=true puts the service in a
  user namespace the account has no capabilities in, which is what stops
  the account from ptrace-ing into it.
- nft.py lets the direct ports out only for a socket in that unit's
  cgroup (`socket cgroupv2 level 3 @syncthing_out`), and only for an
  account allowed to have it. nft resolves a cgroup path to its id when
  the element is added and refuses a path that does not exist, so the
  ruleset carries the sets empty and the elements are added once the
  service is running: by the unit's own ExecStartPost (kosher-syncthing-net,
  below), which covers a restart, and by kosherd after every ruleset load
  (apply), which covers the table being replaced.

Nothing here touches the account's synced folders or Syncthing's own
config and keys: removing the app stops the service and takes away the
unit, the launcher and the build, and leaves ~/.local/state/syncthing.
"""

from __future__ import annotations

import json
import logging
import os
import pwd
import shutil
import socket
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Callable

from . import appaccess, nixapps

log = logging.getLogger(__name__)

REF = "nixpkgs#syncthing"
NAME = "Syncthing"

# The direct ports (nft.py). Out: TCP 22000 (BEP), 22067 (relays), UDP
# 22000 (QUIC) and 21027 (local discovery). In, from the LAN only and only
# with the account's setting on: the account's own listening port and
# local discovery.
OUT_TCP = (22000, 22067)
OUT_UDP = (21027, 22000)
DISCOVERY_PORT = 21027
# Global discovery and the relay pool, which the service reaches on 443
# directly (the discovery servers are pinned by device ID, so through the
# proxy they would always fail). dnsmasq puts their addresses in st4/st6.
HOSTS = ("discovery.syncthing.net", "discovery-v4.syncthing.net",
         "discovery-v6.syncthing.net", "relays.syncthing.net")

# Each account gets its own GUI port and listening port, so two accounts
# can run it at once.
GUI_PORT_BASE = 8384
LISTEN_PORT_BASE = 22000
PORT_SLOTS = 100

UNIT_PREFIX = "syncthing"
# The unit's cgroup, as nft names it: below the cgroup2 root,
# system.slice (1) / system-syncthing.slice (2) / syncthing@<user>.service (3).
CGROUP_LEVEL = 3
CGROUP_ROOT = Path("/sys/fs/cgroup")
SET_OUT = "syncthing_out"
SET_LAN = "syncthing_lan"
TABLE = ("inet", "kosher")

STATE_DIR = Path("/var/lib/kosher/syncthing")
REGISTRY = STATE_DIR / "accounts.json"
DROPIN_ROOT = Path("/etc/systemd/system")
DROPIN_NAME = "50-kosheros.conf"
# What the ExecStartPost helper reads: which accounts' services get which
# sets. Written by kosherd on every apply.
RUN_STATE = Path("/run/kosher/syncthing.json")
LAUNCHER = "kosheros-syncthing.desktop"
MARK = "X-KosherOS-Syncthing"
CONFIG_DIR = ".local/state/syncthing"


class SyncthingError(Exception):
    pass


# -- names -------------------------------------------------------------------

def is_syncthing(ref: str) -> bool:
    return ref == REF


def package(ref: str) -> str:
    if not is_syncthing(ref):
        raise SyncthingError(f"{ref!r} is not Syncthing")
    return "syncthing"


def unit(username: str) -> str:
    return f"{UNIT_PREFIX}@{username}.service"


def cgroup_path(username: str) -> str:
    return f"system.slice/system-{UNIT_PREFIX}.slice/{unit(username)}"


# -- who may run it ----------------------------------------------------------

def allowed(user) -> bool:
    """May this account's Syncthing run and use the direct ports? An
    account with internet, not blocking it by name, and with the
    approval it needs (none for an administrator or an unfiltered one)."""
    if getattr(user, "mode", "none") == "none":
        return False
    if REF in set(getattr(user, "blocked_apps", ()) or ()):
        return False
    return appaccess.individually_approved(user, REF)


def run_state(policy) -> dict:
    """Which accounts' services go in which set, by username."""
    out = sorted(u.username for u in policy.users if allowed(u))
    lan = sorted(u.username for u in policy.users if allowed(u) and u.syncthing_lan)
    return {"out": out, "lan": lan}


# -- the registry: ports, by account -------------------------------------------

def load_registry(path: Path | None = None) -> dict:
    try:
        doc = json.loads((path or REGISTRY).read_text())
    except (OSError, ValueError):
        return {}
    return doc if isinstance(doc, dict) else {}


def _save_registry(reg: dict) -> None:
    REGISTRY.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=REGISTRY.parent)
    with os.fdopen(fd, "w") as f:
        json.dump(reg, f, indent=2, sort_keys=True)
        f.write("\n")
    os.chmod(tmp, 0o600)
    os.rename(tmp, REGISTRY)


def nft_inputs(registry: dict | None = None) -> dict:
    """What nft.render needs: each account's GUI and listening port, by uid."""
    reg = load_registry() if registry is None else registry
    gui, listen = {}, {}
    for entry in reg.values():
        try:
            uid = int(entry["uid"])
            gui[uid] = int(entry["gui"])
            listen[uid] = int(entry["listen"])
        except (KeyError, TypeError, ValueError):
            continue
    return {"gui": gui, "listen": listen}


def _port_free(port: int, host: str) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((host, port))
        except OSError:
            return False
    return True


def pick_ports(reg: dict, username: str,
               free: Callable[[int, str], bool] | None = None) -> tuple[int, int]:
    """The account's GUI and listening ports: what it had, or the lowest
    pair nobody has recorded and nothing is listening on."""
    free = free or _port_free
    mine = reg.get(username)
    if mine and "gui" in mine and "listen" in mine:
        return int(mine["gui"]), int(mine["listen"])
    taken_gui = {int(e.get("gui", 0)) for n, e in reg.items() if n != username}
    taken_listen = {int(e.get("listen", 0)) for n, e in reg.items() if n != username}

    def first(base: int, taken: set, host: str) -> int:
        for port in range(base, base + PORT_SLOTS):
            if port not in taken and free(port, host):
                return port
        raise SyncthingError("no free port for Syncthing")

    return (first(GUI_PORT_BASE, taken_gui, "127.0.0.1"),
            first(LISTEN_PORT_BASE, taken_listen, "0.0.0.0"))


# -- the pieces on disk ------------------------------------------------------

def dropin_path(username: str) -> Path:
    return DROPIN_ROOT / f"{unit(username)}.d" / DROPIN_NAME


def gcroot(username: str) -> Path:
    return STATE_DIR / f"{username}.bin"


def config_dir(pw) -> Path:
    return Path(pw.pw_dir) / CONFIG_DIR


def launcher_path(pw) -> Path:
    return Path(pw.pw_dir) / ".local/share/applications" / LAUNCHER


def render_dropin(store: str, closure: list[str], home: Path, gui: int) -> str:
    """The instance's ExecStart and what it may execute.

    The binary by its store path (read-only, content-addressed: nothing
    the account can swap). NoExecPaths=/ with ExecPaths= the closure is
    what keeps Syncthing from starting anything else inside its cgroup.
    """
    binary = f"{store}/bin/syncthing"
    return (
        "# Written by kosherd (syncthing.py). DO NOT EDIT.\n"
        "[Service]\n"
        "ExecStart=\n"
        f"ExecStart={binary} serve --no-browser --no-restart --no-upgrade "
        f"--home={home} --gui-address=http://127.0.0.1:{gui}\n"
        f"ExecPaths={' '.join(sorted(set(closure) | {store}))}\n"
    )


def render_launcher(gui: int, icon: str = "") -> str:
    return (
        "[Desktop Entry]\n"
        "Type=Application\n"
        f"Name={NAME}\n"
        "Comment=Keep folders the same on your computers and phone\n"
        f"Exec=xdg-open http://127.0.0.1:{gui}/\n"
        f"Icon={icon or 'folder-sync'}\n"
        "Terminal=false\n"
        "Categories=Utility;FileTools;\n"
        f"{MARK}=1\n"
    )


def set_listen_port(config: Path, port: int) -> bool:
    """Point a freshly generated config's listen addresses at `port`.

    Only a "default" listen address is changed: one the person set in
    Syncthing is theirs. Returns whether anything was written."""
    try:
        tree = ET.parse(config)
    except (OSError, ET.ParseError):
        return False
    options = tree.getroot().find("options")
    if options is None:
        return False
    current = [e for e in options.findall("listenAddress")]
    if [e.text for e in current] != ["default"]:
        return False
    for e in current:
        options.remove(e)
    for address in (f"tcp://0.0.0.0:{port}", f"quic://0.0.0.0:{port}",
                    "dynamic+https://relays.syncthing.net/endpoint"):
        el = ET.SubElement(options, "listenAddress")
        el.text = address
    tree.write(config, encoding="utf-8", xml_declaration=False)
    return True


# -- commands, kept apart so the tests can replace them ------------------------

def _run(argv: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, **kw)


def _systemctl(*args: str) -> subprocess.CompletedProcess:
    return _run(["systemctl", *args])


def build(username: str) -> tuple[str, list[str]]:
    """Build nixpkgs#syncthing as root, held by a GC root of the account's
    own, and say where it is and what it needs to run."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    res = _run(["env", f"NIX_SSL_CERT_FILE={nixapps.CA_BUNDLE}", "nix", "build",
                "--print-out-paths", "--out-link", str(gcroot(username)), REF],
               timeout=1800)
    if res.returncode != 0:
        tail = (res.stderr or "").strip().splitlines()
        reason = next((l for l in reversed(tail) if l.startswith("error")),
                      tail[-1] if tail else f"nix exited {res.returncode}")
        raise SyncthingError(reason.removeprefix("error:").strip())
    store = os.path.realpath(gcroot(username))
    if not store.startswith("/nix/store/") or not os.access(f"{store}/bin/syncthing", os.X_OK):
        raise SyncthingError(f"the build of {REF} has no bin/syncthing ({store})")
    res = _run(["nix", "path-info", "--recursive", store])
    closure = [p for p in (res.stdout or "").split() if p.startswith("/nix/store/")]
    if res.returncode != 0 or not closure:
        raise SyncthingError(f"could not list what {store} needs to run")
    return store, closure


def _as_user(pw, argv: list[str]) -> subprocess.CompletedProcess:
    return _run(["runuser", "-u", pw.pw_name, "--", "env", "-i",
                 f"HOME={pw.pw_dir}", f"USER={pw.pw_name}", "PATH=/usr/bin:/bin", *argv])


def _write_owned(path: Path, text: str, pw) -> None:
    nixapps._make_dirs_as(path.parent, pw)
    path.write_text(text)
    os.chown(path, pw.pw_uid, pw.pw_gid)


def _icon(store: str) -> str:
    for rel in ("share/icons/hicolor/scalable/apps/syncthing.svg",
                "share/icons/hicolor/512x512/apps/syncthing.png",
                "share/icons/hicolor/256x256/apps/syncthing.png"):
        if os.path.exists(f"{store}/{rel}"):
            return f"{store}/{rel}"
    return ""


def start(username: str) -> None:
    """Start the account's service. Its ExecStartPost puts it in the
    firewall's sets; kosherd's next apply does too, whichever is later."""
    _systemctl("reset-failed", unit(username))
    res = _systemctl("start", unit(username))
    if res.returncode != 0:
        raise SyncthingError(f"{unit(username)} did not start: "
                             f"{(res.stderr or '').strip()}")
    add_to_sets(username, run_state_of(username))


def stop(username: str) -> None:
    res = _systemctl("stop", unit(username))
    if res.returncode != 0 and "not loaded" not in (res.stderr or ""):
        raise SyncthingError(f"{unit(username)} did not stop: {(res.stderr or '').strip()}")


def is_active(username: str) -> bool:
    return _systemctl("is-active", "--quiet", unit(username)).returncode == 0


# -- the firewall sets -------------------------------------------------------

def write_run_state(state: dict) -> None:
    RUN_STATE.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=RUN_STATE.parent)
    with os.fdopen(fd, "w") as f:
        json.dump(state, f)
    os.chmod(tmp, 0o600)
    os.rename(tmp, RUN_STATE)


def run_state_of(username: str, path: Path | None = None) -> list[str]:
    """The sets this account's service belongs in, from the run state."""
    try:
        state = json.loads((path or RUN_STATE).read_text())
    except (OSError, ValueError):
        return []
    sets = []
    if username in state.get("out", []):
        sets.append(SET_OUT)
        if username in state.get("lan", []):
            sets.append(SET_LAN)
    return sets


def add_to_sets(username: str, sets: list[str]) -> list[str]:
    """Put the running service's cgroup in `sets`. Only a cgroup that
    exists: nft refuses a path it cannot resolve. Returns the sets added."""
    if not sets or not (CGROUP_ROOT / cgroup_path(username)).is_dir():
        return []
    added = []
    for name in sets:
        res = _run(["nft", "add", "element", *TABLE, name,
                    f'{{ "{cgroup_path(username)}" }}'])
        if res.returncode == 0:
            added.append(name)
        else:
            log.warning("syncthing: %s not added to %s: %s", username, name,
                        (res.stderr or "").strip())
    return added


def apply(policy) -> None:
    """After every ruleset load: record who gets which set, start the
    services that should run and stop the ones that should not, and put
    the running ones back in the (freshly emptied) sets."""
    state = run_state(policy)
    write_run_state(state)
    reg = load_registry()
    by_name = {u.username: u for u in policy.users}
    for username, entry in reg.items():
        user = by_name.get(username)
        want = (user is not None and user.uid == entry.get("uid")
                and username in state["out"] and dropin_path(username).exists())
        try:
            if want and not is_active(username):
                start(username)
            elif not want and is_active(username):
                stop(username)
        except SyncthingError as e:
            log.warning("syncthing: %s", e)
        if want:
            add_to_sets(username, run_state_of(username))


# -- the Store's side: install, upgrade, remove ---------------------------------

def installed(uid: int) -> set[str]:
    for username, entry in load_registry().items():
        if entry.get("uid") == uid and dropin_path(username).exists():
            return {REF}
    return set()


def install(ref: str, uid: int, on_progress, as_root: bool = False) -> None:
    """Build it, give the account its ports, config, unit and launcher,
    and start it. Any step that fails undoes the ones before it; the
    config directory is removed only if this install made it."""
    package(ref)
    pw = pwd.getpwuid(uid)
    username = pw.pw_name
    undo: list[Callable[[], None]] = []
    reg = load_registry()
    try:
        on_progress(ref, 0, "Downloading…")
        had_root = gcroot(username).is_symlink()
        store, closure = build(username)
        if not had_root:
            undo.append(lambda: gcroot(username).unlink(missing_ok=True))

        gui, listen = pick_ports(reg, username)
        before = reg.get(username)
        reg[username] = {"uid": uid, "gui": gui, "listen": listen}
        _save_registry(reg)
        undo.append(lambda: _restore_registry(username, before))

        on_progress(ref, 0, "Setting up…")
        home = config_dir(pw)
        if not (home / "config.xml").exists():
            made = not home.exists()
            nixapps._make_dirs_as(home, pw)
            if made:
                undo.append(lambda: shutil.rmtree(home, ignore_errors=True))
            res = _as_user(pw, [f"{store}/bin/syncthing", "generate", f"--home={home}",
                                "--no-port-probing"])
            if res.returncode != 0:
                raise SyncthingError("could not make Syncthing's settings: "
                                     + (res.stderr or "").strip()[-300:])
            if set_listen_port(home / "config.xml", listen):
                os.chown(home / "config.xml", pw.pw_uid, pw.pw_gid)

        dropin = dropin_path(username)
        old = dropin.read_text() if dropin.exists() else None
        dropin.parent.mkdir(parents=True, exist_ok=True)
        dropin.write_text(render_dropin(store, closure, home, gui))
        undo.append(lambda: _restore_file(dropin, old))
        _systemctl("daemon-reload")
        undo.append(lambda: _systemctl("daemon-reload"))

        launcher = launcher_path(pw)
        had_launcher = launcher.exists()
        _write_owned(launcher, render_launcher(gui, _icon(store)), pw)
        if not had_launcher:
            undo.append(lambda: launcher.unlink(missing_ok=True))

        on_progress(ref, 0, "Starting…")
        undo.append(lambda: stop(username))
        start(username)
    except Exception:
        for step in reversed(undo):
            try:
                step()
            except Exception:  # noqa: BLE001 - undo as much as can be undone
                log.exception("syncthing: rolling back %s's install", username)
        raise


def _restore_registry(username: str, before) -> None:
    reg = load_registry()
    if before is None:
        reg.pop(username, None)
    else:
        reg[username] = before
    _save_registry(reg)


def _restore_file(path: Path, text: str | None) -> None:
    if text is None:
        path.unlink(missing_ok=True)
        try:
            path.parent.rmdir()
        except OSError:
            pass
    else:
        path.write_text(text)


def upgrade(ref: str, uid: int, on_progress, as_root: bool = False) -> None:
    """A newer Syncthing from nixpkgs: rebuild, point the unit at it and
    restart a running service. The old build stays until the unit runs
    the new one."""
    package(ref)
    pw = pwd.getpwuid(uid)
    reg = load_registry()
    entry = reg.get(pw.pw_name)
    if not entry:
        raise SyncthingError("Syncthing is not installed for this account")
    on_progress(ref, 0, "Updating…")
    store, closure = build(pw.pw_name)
    dropin = dropin_path(pw.pw_name)
    dropin.parent.mkdir(parents=True, exist_ok=True)
    dropin.write_text(render_dropin(store, closure, config_dir(pw), int(entry["gui"])))
    _systemctl("daemon-reload")
    _write_owned(launcher_path(pw), render_launcher(int(entry["gui"]), _icon(store)), pw)
    if is_active(pw.pw_name):
        _systemctl("restart", unit(pw.pw_name))
        add_to_sets(pw.pw_name, run_state_of(pw.pw_name))


def remove(ref: str, uid: int, on_progress, as_root: bool = False) -> None:
    package(ref)
    pw = pwd.getpwuid(uid)
    on_progress(ref, 0, "Removing…")
    forget(pw.pw_name, home=Path(pw.pw_dir))


def forget(username: str, home: Path | None = None) -> None:
    """Stop the service and take away everything kosherd made for it:
    the unit's drop-in, the launcher, the build's GC root, the ports.
    Syncthing's own config and the synced folders stay. Also what an
    account's removal calls, before the account goes."""
    dropin = dropin_path(username)
    if username not in load_registry() and not dropin.exists():
        return  # never had it
    stop(username)
    dropin.unlink(missing_ok=True)
    try:
        dropin.parent.rmdir()
    except OSError:
        pass
    _systemctl("daemon-reload")
    if home is not None:
        launcher = home / ".local/share/applications" / LAUNCHER
        try:
            if MARK in launcher.read_text():
                launcher.unlink()
        except OSError:
            pass
    gcroot(username).unlink(missing_ok=True)
    reg = load_registry()
    if reg.pop(username, None) is not None:
        _save_registry(reg)


def exists(ref: str) -> bool | None:
    return nixapps.exists(ref)


# -- kosher-syncthing-net: the unit's ExecStartPost ---------------------------

def net_main(argv: list[str] | None = None) -> int:
    """`kosher-syncthing-net <user>`, run as root by the unit after its
    process starts: put its cgroup in the sets kosherd last said it gets.
    Never fails the unit; a service without the sets is one that cannot
    reach its peers, which is the safe way to be wrong."""
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("usage: kosher-syncthing-net <user>", file=sys.stderr)
        return 0
    username = args[0]
    added = add_to_sets(username, run_state_of(username))
    print(f"{unit(username)}: " + (", ".join(added) if added else "no direct ports"))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(net_main())
