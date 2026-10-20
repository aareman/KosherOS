"""Each person's own key remapping, through keyd.

keyd is the system's key remapper: a root daemon that reads the keyboards
in /dev/input and sends what they should mean out through a virtual one.
It is the only thing on the machine that reads raw input. Accounts get no
`input` or `uinput` group (what kanata or xremap would need), because an
account that can read /dev/input can read the guardian password as a
parent types it.

Accounts do not get keyd's socket either. keyd's `command()` action runs a
shell command as the daemon, which is root, and a client on the socket can
also type into whatever has the keyboard; keyd's own manual says anyone on
the socket should be treated as root. So nobody is in the `keyd` group and
kosherd stands in between:

- A person writes ~/.config/keyd/default.conf in keyd's own syntax.
- kosher-keyboard, in their session (kosher-keyboard.service), sends the
  file's text to kosherd when they sign in and whenever they save it.
- kosherd checks it here (`prepare`): no `command()`, includes only from
  keyd's shipped files, nothing big enough to trip keyd's parser limits,
  and its own `[ids]` section so it applies to every keyboard. Then
  `keyd check` parses it, as nobody, before the daemon ever sees it.
- kosherd keeps each signed-in person's accepted config in memory, and
  whichever person's session is active on the seat (logind) is the one in
  force: kosherd writes it to /run/keyd/default.conf and runs
  `keyd reload`. The login screen, a lock-screen-less switch to another
  person, and an account with no file all get the machine's own config,
  /etc/keyd/default.conf, which remaps nothing.

Why a whole file and `keyd reload` rather than `keyd bind`: bind can only
change keys in layers the loaded config already has. It cannot add a
layer, an alias or a [global] setting, which is most of what people use
keyd for. Reloading a file takes all of it, and putting the machine's file
back undoes all of it, with nothing left over for the next person.

keyd compiled for KosherOS reads /run/keyd (not /etc/keyd), so nothing
here writes /etc and a reboot always starts from the machine's config;
keyd.service copies /etc/keyd/default.conf there each time it starts.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import tempfile
from pathlib import Path

log = logging.getLogger("kosherd.keyboard")

# Where keyd (built with CONFIG_DIR=/run/keyd) looks, and the machine's own
# config that keyd.service copies there at every start.
SYSTEM_DIR = Path("/run/keyd")
BASE_CONFIG = Path("/etc/keyd/default.conf")
# keyd's shipped layouts and data, the only place an include may point.
DATA_DIR = Path("/usr/share/keyd")
# The person's file, relative to their home.
USER_CONFIG = Path(".config/keyd/default.conf")

# Well inside keyd's own limits (64 KiB per file with its includes, 4096
# bytes a line, 32 sections, 1024 entries a section), which it enforces
# with assert(): a file past them aborts the daemon instead of failing.
MAX_BYTES = 32 * 1024
MAX_LINE = 1000
MAX_LINES = 2000
MAX_SECTIONS = 30
MAX_INCLUDES = 8

# Prepended to every person's config: it applies to every keyboard. Their
# own [ids] section, if they wrote one, is blanked line for line so keyd's
# line numbers still point at their file once the header is taken off.
HEADER = "[ids]\n*\n"
HEADER_LINES = HEADER.count("\n")

# command() anywhere in a value, also inside overload(...) or a macro, and
# also with spaces before the bracket in case a later keyd strips them.
_COMMAND = re.compile(r"command\s*\(", re.IGNORECASE)
_INCLUDE_NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_./-]*")
# Tab is allowed; every other control character is refused.
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


class KeymapError(ValueError):
    """A person's keyboard file that will not be applied, and why."""


def _section_name(stripped: str) -> str | None:
    """The section a line opens, read the way keyd's ini parser reads it
    (a line that starts with '[' and ends with ']', name taken verbatim),
    or None if it opens none."""
    if len(stripped) >= 2 and stripped[0] == "[" and stripped[-1] == "]":
        return stripped[1:-1]
    return None


def _check_include(name: str, number: int, data_dir: Path) -> None:
    if (not _INCLUDE_NAME.fullmatch(name) or ".." in name.split("/")):
        raise KeymapError(f"line {number}: include {name!r} is not a file keyd ships")
    root = data_dir.resolve()
    target = (data_dir / name).resolve()
    if root not in target.parents or not target.is_file():
        raise KeymapError(f"line {number}: include {name!r} is not a file keyd ships "
                          f"(see {data_dir}/layouts)")


def prepare(text: str, *, data_dir: Path = DATA_DIR) -> str:
    """The system config for one person's file, or KeymapError.

    The result keeps the person's lines in place (only their [ids] section
    is blanked) under HEADER, so an error keyd reports at line N of the
    result is at line N - HEADER_LINES of their file.
    """
    if len(text.encode("utf-8")) > MAX_BYTES:
        raise KeymapError(f"the file is larger than {MAX_BYTES // 1024} KB")
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    if len(lines) > MAX_LINES:
        raise KeymapError(f"the file has more than {MAX_LINES} lines")

    out: list[str] = []
    in_ids = False
    sections = includes = 0
    for number, raw in enumerate(lines, 1):
        line = raw.rstrip("\r")
        if len(line) > MAX_LINE:
            raise KeymapError(f"line {number} is longer than {MAX_LINE} characters")
        if _CONTROL.search(line):
            raise KeymapError(f"line {number} has a control character in it")
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            out.append(line)
            continue
        # keyd reads includes before its ini parser, from the raw line.
        if line.startswith("include "):
            includes += 1
            if includes > MAX_INCLUDES:
                raise KeymapError(f"line {number}: more than {MAX_INCLUDES} includes")
            _check_include(line[len("include "):].strip(), number, data_dir)
            out.append("" if in_ids else line)
            continue
        name = _section_name(stripped)
        if name is not None:
            in_ids = name == "ids"
            if in_ids:
                out.append("")
                continue
            sections += 1
            if sections > MAX_SECTIONS:
                raise KeymapError(f"line {number}: more than {MAX_SECTIONS} sections")
            out.append(line)
            continue
        if in_ids:
            out.append("")
            continue
        if _COMMAND.search(line):
            raise KeymapError(
                f"line {number}: command() is not allowed here, because keyd "
                "would run it for the whole computer, not for you")
        out.append(line)
    return HEADER + "".join(f"{line}\n" for line in out)


# "\tWARNING: /run/keyd/check/x.conf:6: invalid key or action"
_ERROR_AT = re.compile(r"^\s*(?:WARNING|ERROR):\s*(?P<path>[^:\s]+):(?P<line>\d+):\s*(?P<what>.*)$")


def explain_check(output: str, checked: str) -> list[str]:
    """keyd check's complaints about `checked`, as lines of the person's own
    file. Complaints about an included file name that file."""
    found = []
    for raw in output.splitlines():
        m = _ERROR_AT.match(raw)
        if not m:
            continue
        what = m.group("what").strip()
        if m.group("path") == checked:
            number = int(m.group("line")) - HEADER_LINES
            found.append(f"line {number}: {what}" if number > 0 else what)
        else:
            found.append(f"{m.group('path')} line {m.group('line')}: {what}")
    return found


# keyd check runs as nobody: a parser bug met while checking a person's
# file is met without root.
CHECK_AS = {"user": "nobody", "group": "nobody", "extra_groups": []}


def _write(path: Path, content: str, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


class Keyboards:
    """Whose keyboard config is in force, kept in kosherd.

    `configs` holds each signed-in person's accepted config (already
    prepared) by uid. `refresh(active_uid)` puts the active person's in
    force, or the machine's when nobody's is wanted. It compares with the
    file keyd actually reads rather than remembering what it last wrote,
    so after keyd restarts (and puts the machine's config back) the next
    refresh notices.
    """

    def __init__(self, *, system_dir: Path = SYSTEM_DIR, base: Path = BASE_CONFIG,
                 data_dir: Path = DATA_DIR, run=subprocess.run) -> None:
        self.system_dir = system_dir
        self.base = base
        self.data_dir = data_dir
        self.run = run
        self.configs: dict[int, str] = {}

    @property
    def live(self) -> Path:
        return self.system_dir / "default.conf"

    def base_config(self) -> str:
        try:
            return self.base.read_text(encoding="utf-8")
        except OSError:
            log.warning("keyboard: %s is missing; using one that remaps nothing", self.base)
            return HEADER

    def check(self, prepared: str) -> None:
        """keyd's own parse of a prepared config, or KeymapError."""
        check_dir = self.system_dir / "check"
        check_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(check_dir, 0o755)
        path = check_dir / "candidate.conf"
        _write(path, prepared)
        try:
            result = self.run(["keyd", "check", str(path)], capture_output=True,
                              text=True, timeout=10, **CHECK_AS)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise KeymapError(f"keyd could not check the file: {e}") from e
        finally:
            path.unlink(missing_ok=True)
        if result.returncode != 0:
            problems = explain_check((result.stdout or "") + (result.stderr or ""), str(path))
            raise KeymapError("; ".join(problems) if problems
                              else "keyd could not read the file")

    def set(self, uid: int, text: str, active_uid: int | None) -> bool:
        """A person's file, as they saved it. Empty means they have none.

        A file that fails changes nothing: whatever of theirs was in force
        stays in force. Returns whether keyd was reloaded."""
        if not text.strip():
            self.configs.pop(uid, None)
        else:
            prepared = prepare(text, data_dir=self.data_dir)
            self.check(prepared)
            self.configs[uid] = prepared
        return self.refresh(active_uid)

    def forget_all_but(self, signed_in: set[int]) -> None:
        """Drop the configs of people with no session left."""
        for uid in list(self.configs):
            if uid not in signed_in:
                del self.configs[uid]

    def wanted(self, active_uid: int | None) -> str:
        if active_uid is not None and active_uid in self.configs:
            return self.configs[active_uid]
        return self.base_config()

    def refresh(self, active_uid: int | None) -> bool:
        """Put the right config in force. Returns whether keyd was reloaded."""
        content = self.wanted(active_uid)
        try:
            if self.live.read_text(encoding="utf-8") == content:
                return False
        except OSError:
            pass
        _write(self.live, content)
        try:
            result = self.run(["keyd", "reload"], capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.TimeoutExpired) as e:
            log.warning("keyboard: keyd reload failed: %s", e)
            return False
        if result.returncode != 0:
            log.warning("keyboard: keyd reload failed: %s",
                        (result.stderr or result.stdout or "").strip())
            return False
        log.info("keyboard: in force for %s",
                 "uid %d" % active_uid if active_uid in self.configs else "nobody")
        return True


# ---- in the person's session ------------------------------------------------

def read_own(home: Path) -> str:
    """The person's file, or "" if they have none."""
    try:
        return (home / USER_CONFIG).read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""
    except (OSError, UnicodeDecodeError) as e:
        raise KeymapError(f"could not read {USER_CONFIG}: {e}") from e


def main(argv=None) -> int:  # pragma: no cover - needs a session and kosherd
    """kosher-keyboard: apply this account's keyd file.

    `kosher-keyboard` sends the file once and says what happened, for a
    person at a terminal. `kosher-keyboard --watch` is what the session
    runs: it sends the file when kosherd appears (at sign-in, and again if
    kosherd restarts) and every time the file is saved, and shows a
    notification when keyd refuses it.
    """
    import argparse
    import sys

    import gi

    gi.require_version("Gio", "2.0")
    from gi.repository import Gio, GLib

    from . import BUS_NAME, OBJECT_PATH

    p = argparse.ArgumentParser(prog="kosher-keyboard",
                                description="apply ~/.config/keyd/default.conf")
    p.add_argument("--watch", action="store_true",
                   help="keep running, and apply the file each time it is saved")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(name)s: %(message)s",
                        stream=sys.stderr)
    home = Path.home()
    system = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)

    def send() -> str | None:
        """Apply the file; None when it worked, else what went wrong."""
        try:
            text = read_own(home)
            system.call_sync(BUS_NAME, OBJECT_PATH, "org.kosherlinux.Daemon1.Profiles",
                             "SetMyKeyboard", GLib.Variant("(s)", (text,)), None,
                             Gio.DBusCallFlags.NONE, 30000, None)
        except KeymapError as e:
            return str(e)
        except GLib.Error as e:
            return Gio.DBusError.strip_remote_error(e) or e.message
        return None

    if not args.watch:
        problem = send()
        if problem:
            print(f"Not applied: {problem}", file=sys.stderr)
            return 1
        print("Applied." if read_own(home) else "No file, so no remaps.")
        return 0

    session = Gio.bus_get_sync(Gio.BusType.SESSION, None)

    def notify(problem: str) -> None:
        try:
            session.call_sync(
                "org.freedesktop.Notifications", "/org/freedesktop/Notifications",
                "org.freedesktop.Notifications", "Notify",
                GLib.Variant("(susssasa{sv}i)", (
                    "KosherOS", 0, "input-keyboard-symbolic",
                    "Your keyboard file was not applied",
                    f"{problem}\n\nThe keys work as they did before you saved it.",
                    [], {}, 20000)),
                GLib.VariantType("(u)"), Gio.DBusCallFlags.NONE, 5000, None)
        except GLib.Error as e:
            log.warning("could not show the notification: %s", e.message)

    pending = {"id": 0}

    def apply_now() -> bool:
        pending["id"] = 0
        problem = send()
        if problem:
            log.warning("not applied: %s", problem)
            notify(problem)
        else:
            log.info("applied")
        return False

    def soon(*_args) -> None:
        # Editors save in bursts (a temp file, a rename, a chmod).
        if pending["id"]:
            GLib.source_remove(pending["id"])
        pending["id"] = GLib.timeout_add(300, apply_now)

    folder = Gio.File.new_for_path(str((home / USER_CONFIG).parent))
    try:
        folder.make_directory_with_parents(None)
    except GLib.Error:
        pass
    monitor = folder.monitor_directory(Gio.FileMonitorFlags.WATCH_MOVES, None)

    def changed(_monitor, child, other, _event) -> None:
        names = {f.get_basename() for f in (child, other) if f is not None}
        if USER_CONFIG.name in names:
            soon()

    monitor.connect("changed", changed)
    Gio.bus_watch_name(Gio.BusType.SYSTEM, BUS_NAME, Gio.BusNameWatcherFlags.NONE,
                       lambda *_a: soon(), None)
    GLib.MainLoop().run()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
