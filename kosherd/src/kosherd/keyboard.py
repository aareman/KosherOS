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

- A person writes one or more files in ~/.config/keyd/ (default.conf, and
  e.g. one per keyboard, each with an [ids] section naming its devices),
  in keyd's own syntax. app.conf there is keyd-application-mapper's, not
  a keyboard file.
- kosher-keyboard, in their session (kosher-keyboard.service), sends the
  files to kosherd when they sign in and whenever one is saved.
- kosherd checks them here (`prepare_set`): [ids] entries only in keyd's
  id syntax, at most one file for every keyboard, no two files naming the
  same device, includes only from keyd's shipped files, nothing big
  enough to trip keyd's parser limits. Then `keyd check` parses the whole
  set, as nobody, before the daemon ever sees it.
- `key = command(...)` does not reach keyd. kosherd keeps the command,
  and the key runs `kosher-keyd-run <uid> <n>` instead: a fixed program,
  two numbers, nothing of the person's text. That program looks the
  command up, checks the person is the one on the screen, and runs it AS
  THEM in their own session (`run_main`). It never runs their text as
  root, so a command gives nobody anything they could not do from their
  own terminal.
- kosherd keeps each signed-in person's accepted set in memory, and the
  set of whichever person's session is active on the seat (logind) is the
  one in force: kosherd writes it into /run/keyd and runs `keyd reload`.
  The login screen, a switch to another person, and an account with no
  files all get the machine's own config, /etc/keyd/default.conf, which
  remaps nothing.

Why whole files and `keyd reload` rather than `keyd bind`: bind can only
change keys in layers the loaded config already has. It cannot add a
layer, an alias or a [global] setting, which is most of what people use
keyd for. Reloading takes all of it, and putting the machine's file back
undoes all of it, with nothing left over for the next person.

Per-application keys (app.conf) still use bind, as keyd-application-
mapper does upstream, but through kosherd: the mapper runs in the
person's session with KEYD_BIN pointing at kosher-keyd-bind, which hands
its bindings to kosherd (SetMyAppKeys). They are checked like a file's
lines, applied only for the person on the screen, and dropped when
anyone else's session takes it.

keyd compiled for KosherOS reads /run/keyd (not /etc/keyd), so nothing
here writes /etc and a reboot always starts from the machine's config;
keyd.service puts /etc/keyd/default.conf there, alone, each time it
starts.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("kosherd.keyboard")

# Where keyd (built with CONFIG_DIR=/run/keyd) looks, and the machine's own
# config that keyd.service copies there at every start.
SYSTEM_DIR = Path("/run/keyd")
BASE_CONFIG = Path("/etc/keyd/default.conf")
BASE_NAME = "default.conf"
# A person's files go in beside it under this prefix, so no name of theirs
# can replace the machine's file.
LIVE_PREFIX = "person-"
# keyd's shipped layouts and data, the only place an include may point.
DATA_DIR = Path("/usr/share/keyd")
# The person's files, relative to their home.
USER_DIR = Path(".config/keyd")
USER_CONFIG = USER_DIR / "default.conf"
APP_CONF = "app.conf"
# What a key with command() runs, and where kosherd keeps the commands
# it stands for (root only).
RUN_HELPER = "/usr/libexec/kosher-keyd-run"
COMMANDS_DIR = Path("/run/kosher-keyd")

# Well inside keyd's own limits (64 KiB per file with its includes, 4096
# bytes a line, 32 sections, 1024 entries a section, 64 ids and 64
# commands a file, 256 bytes a command), which it enforces with assert():
# a file past them aborts the daemon instead of failing.
MAX_FILES = 8
MAX_BYTES = 32 * 1024
MAX_LINE = 1000
MAX_LINES = 2000
MAX_SECTIONS = 30
MAX_INCLUDES = 8
MAX_IDS = 32
MAX_FILE_COMMANDS = 32
MAX_COMMANDS = 128
MAX_COMMAND = 200
MAX_BINDINGS = 64
MAX_BINDING = 500
# The person's commands run at most this often (seconds). keyd ignores key
# repeat, so this is about a key tapped fast or two keys at once.
MIN_COMMAND_INTERVAL = 0.15

# Put in front of a file with no [ids] section of its own: it applies to
# every keyboard, as default.conf is meant to.
HEADER = "[ids]\n*\n"
HEADER_LINES = HEADER.count("\n")

# command() anywhere in a line, also inside overload(...) or a macro, and
# also with spaces before the bracket in case a later keyd strips them.
_COMMAND = re.compile(r"command\s*\(", re.IGNORECASE)
_INCLUDE_NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_./-]*")
# Tab is allowed; every other control character is refused.
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
_FILE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,31}\.conf")
# keyd's ids: vendor:product, optionally the hash keyd adds (keyd matches
# by prefix, so vendor:product names every device of that model).
_DEVICE = r"[0-9a-f]{4}:[0-9a-f]{4}(?::[0-9a-f]{1,8})?"
_ID_ENTRY = re.compile(rf"\*|-{_DEVICE}|(?:[km]:)?{_DEVICE}")


class KeymapError(ValueError):
    """A person's keyboard files that will not be applied, and why."""


@dataclass
class Commands:
    """The commands behind a person's command() keys, by number."""

    table: dict[int, str] = field(default_factory=dict)

    def add(self, body: str, where: str) -> int:
        for number, known in self.table.items():
            if known == body:
                return number
        if len(self.table) >= MAX_COMMANDS:
            raise KeymapError(f"{where}: more than {MAX_COMMANDS} different commands")
        number = len(self.table)
        self.table[number] = body
        return number


@dataclass
class Prepared:
    """One of a person's files, ready for /run/keyd."""

    name: str
    text: str
    offset: int        # lines put in front, to take off keyd's line numbers
    wildcard: bool     # applies to every keyboard
    ids: list[str]     # devices it names, without k:/m:


@dataclass
class PersonSet:
    files: dict[str, Prepared]
    commands: Commands

    @property
    def wildcard(self) -> bool:
        return any(p.wildcard for p in self.files.values())


def _section_name(stripped: str) -> str | None:
    """The section a line opens, read the way keyd's ini parser reads it
    (a line that starts with '[' and ends with ']', name taken verbatim),
    or None if it opens none."""
    if len(stripped) >= 2 and stripped[0] == "[" and stripped[-1] == "]":
        return stripped[1:-1]
    return None


def _check_include(name: str, where: str, data_dir: Path) -> None:
    if (not _INCLUDE_NAME.fullmatch(name) or ".." in name.split("/")):
        raise KeymapError(f"{where}: include {name!r} is not a file keyd ships")
    root = data_dir.resolve()
    target = (data_dir / name).resolve()
    if root not in target.parents or not target.is_file():
        raise KeymapError(f"{where}: include {name!r} is not a file keyd ships "
                          f"(see {data_dir}/layouts)")


def _binding(line: str, where: str, uid: int, commands: Commands,
             counted: list[int] | None = None) -> str:
    """One `key = action` line with any command() swapped for the helper.

    command() is taken only as a key's whole action; anywhere else it is
    refused, because the text inside would reach keyd, which runs it as
    root."""
    key, eq, value = line.partition("=")
    if eq and value.strip().startswith("command(") and value.strip().endswith(")") \
            and not _COMMAND.search(key):
        body = value.strip()[len("command("):-1].strip()
        if not body:
            raise KeymapError(f"{where}: command() is empty")
        if len(body) > MAX_COMMAND:
            raise KeymapError(f"{where}: the command is longer than {MAX_COMMAND} characters")
        if counted is not None:
            counted[0] += 1
            if counted[0] > MAX_FILE_COMMANDS:
                raise KeymapError(f"{where}: more than {MAX_FILE_COMMANDS} command() keys")
        number = commands.add(body, where)
        return f"{key.rstrip()} = command({RUN_HELPER} {uid} {number})"
    if _COMMAND.search(line):
        raise KeymapError(f"{where}: command() has to be a key's whole action, "
                          "as in f5 = command(...)")
    return line


def prepare(text: str, *, name: str = "default.conf", uid: int = 0,
            commands: Commands | None = None, data_dir: Path = DATA_DIR) -> Prepared:
    """One person's file for /run/keyd, or KeymapError.

    The result keeps the person's lines in place, so an error keyd reports
    at line N of it is at line N - offset of their file. command() keys are
    rewritten in place to the helper (see the module docstring).
    """
    commands = commands if commands is not None else Commands()
    if len(text.encode("utf-8")) > MAX_BYTES:
        raise KeymapError(f"{name} is larger than {MAX_BYTES // 1024} KB")
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    if len(lines) > MAX_LINES:
        raise KeymapError(f"{name} has more than {MAX_LINES} lines")

    out: list[str] = []
    in_ids = False
    seen_ids = wildcard = False
    ids: list[str] = []
    sections = includes = 0
    counted = [0]
    for number, raw in enumerate(lines, 1):
        where = f"{name} line {number}"
        line = raw.rstrip("\r")
        if len(line) > MAX_LINE:
            raise KeymapError(f"{where} is longer than {MAX_LINE} characters")
        if _CONTROL.search(line):
            raise KeymapError(f"{where} has a control character in it")
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            out.append(line)
            continue
        # keyd reads includes before its ini parser, from the raw line.
        if line.startswith("include "):
            if in_ids:
                raise KeymapError(f"{where}: an include cannot go in [ids]")
            includes += 1
            if includes > MAX_INCLUDES:
                raise KeymapError(f"{where}: more than {MAX_INCLUDES} includes")
            _check_include(line[len("include "):].strip(), where, data_dir)
            out.append(line)
            continue
        section = _section_name(stripped)
        if section is not None:
            in_ids = section == "ids"
            if in_ids:
                if seen_ids:
                    raise KeymapError(f"{where}: a second [ids] section")
                seen_ids = True
            else:
                sections += 1
                if sections > MAX_SECTIONS:
                    raise KeymapError(f"{where}: more than {MAX_SECTIONS} sections")
            out.append(line)
            continue
        if in_ids:
            entry = stripped.lower()
            if not _ID_ENTRY.fullmatch(entry):
                raise KeymapError(
                    f"{where}: {stripped!r} is not a device id; [ids] takes *, "
                    "an id such as 046d:c52b (see kosher-keyboard --list-devices), "
                    "or -046d:c52b to leave one out")
            if entry == "*":
                wildcard = True
            elif not entry.startswith("-"):
                ids.append(entry.split(":", 1)[1] if entry[:2] in ("k:", "m:") else entry)
            if len(ids) > MAX_IDS:
                raise KeymapError(f"{where}: more than {MAX_IDS} devices")
            out.append(entry)
            continue
        out.append(_binding(line, where, uid, commands, counted))
    body = "".join(f"{line}\n" for line in out)
    if not seen_ids:
        return Prepared(name, HEADER + body, HEADER_LINES, True, [])
    return Prepared(name, body, 0, wildcard, ids)


def prepare_set(files: dict[str, str], *, uid: int = 0,
                data_dir: Path = DATA_DIR) -> PersonSet:
    """All of a person's files, checked together, or KeymapError.

    keyd gives a keyboard to the file that names it over a file with `*`,
    and between two files that both name it, or two with `*`, the choice
    is down to directory order. So a set may have one `*` file at most and
    may not name a device twice; then what applies to a keyboard is
    always clear. Files that name no `*` leave the other keyboards to the
    machine's config.
    """
    if len(files) > MAX_FILES:
        raise KeymapError(f"more than {MAX_FILES} files in ~/{USER_DIR}")
    commands = Commands()
    prepared: dict[str, Prepared] = {}
    for name in sorted(files):
        if name == APP_CONF or not _FILE_NAME.fullmatch(name):
            raise KeymapError(f"{name!r} is not a name kosherd takes for a keyboard file "
                              "(letters, digits, - and _, ending in .conf)")
        prepared[name] = prepare(files[name], name=name, uid=uid, commands=commands,
                                 data_dir=data_dir)
    wild = [p.name for p in prepared.values() if p.wildcard]
    if len(wild) > 1:
        raise KeymapError(
            f"{' and '.join(wild)} both apply to every keyboard; give all but one "
            "an [ids] section naming its keyboard")
    named: list[tuple[str, str]] = []
    for p in prepared.values():
        for device in p.ids:
            for other_name, other in named:
                if other_name != p.name and (device.startswith(other) or other.startswith(device)):
                    raise KeymapError(f"{p.name} and {other_name} both name {device}; "
                                      "a keyboard can have one file")
            named.append((p.name, device))
    return PersonSet(prepared, commands)


def prepare_bindings(bindings: list[str], *, uid: int, commands: Commands) -> list[str]:
    """keyd-application-mapper's bind expressions, checked like a file's
    lines (command() to the helper, nothing else of it)."""
    if len(bindings) > MAX_BINDINGS:
        raise KeymapError(f"more than {MAX_BINDINGS} bindings")
    out = []
    for n, expr in enumerate(bindings, 1):
        where = f"{APP_CONF} binding {n}"
        if len(expr) > MAX_BINDING or _CONTROL.search(expr) or "\n" in expr:
            raise KeymapError(f"{where} is too long or has a control character in it")
        if "=" not in expr:
            raise KeymapError(f"{where}: {expr!r} is not key = action")
        out.append(_binding(expr.strip(), where, uid, commands))
    return out


# "\tWARNING: /run/keyd/check/person-x.conf:6: invalid key or action"
_ERROR_AT = re.compile(r"^\s*(?:WARNING|ERROR):\s*(?P<path>[^:\s]+):(?P<line>\d+):\s*(?P<what>.*)$")


def explain_check(output: str, checked: dict[str, Prepared]) -> list[str]:
    """keyd check's complaints, in the person's own files and lines.
    `checked` maps each checked path to its file. Complaints about an
    included file name that file."""
    found = []
    for raw in output.splitlines():
        m = _ERROR_AT.match(raw)
        if not m:
            continue
        what = m.group("what").strip()
        p = checked.get(m.group("path"))
        if p is not None:
            number = int(m.group("line")) - p.offset
            found.append(f"{p.name} line {number}: {what}" if number > 0 else f"{p.name}: {what}")
        else:
            found.append(f"{m.group('path')} line {m.group('line')}: {what}")
    return found


# keyd check runs as nobody: a parser bug met while checking a person's
# files is met without root.
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

    `configs` holds each signed-in person's accepted set by uid.
    `refresh(active_uid)` puts the active person's set in force, or the
    machine's config when nobody's is wanted. It compares with the files
    keyd actually reads rather than remembering what it last wrote, so
    after keyd restarts (and puts the machine's config back alone) the
    next refresh notices.
    """

    def __init__(self, *, system_dir: Path = SYSTEM_DIR, base: Path = BASE_CONFIG,
                 data_dir: Path = DATA_DIR, commands_dir: Path = COMMANDS_DIR,
                 run=subprocess.run) -> None:
        self.system_dir = system_dir
        self.base = base
        self.data_dir = data_dir
        self.commands_dir = commands_dir
        self.run = run
        self.configs: dict[int, PersonSet] = {}
        # Whose app.conf bindings are on top of the loaded config.
        self.bound_uid: int | None = None

    def base_config(self) -> str:
        try:
            return self.base.read_text(encoding="utf-8")
        except OSError:
            log.warning("keyboard: %s is missing; using one that remaps nothing", self.base)
            return HEADER

    # -- the files ----------------------------------------------------------

    def _files_for(self, person: PersonSet | None) -> dict[str, str]:
        files = {}
        if person is None or not person.wildcard:
            files[BASE_NAME] = self.base_config()
        if person is not None:
            for p in person.files.values():
                files[LIVE_PREFIX + p.name] = p.text
        return files

    def wanted(self, active_uid: int | None) -> dict[str, str]:
        return self._files_for(self.configs.get(active_uid) if active_uid is not None else None)

    def live(self) -> dict[str, str]:
        found = {}
        try:
            for path in self.system_dir.glob("*.conf"):
                found[path.name] = path.read_text(encoding="utf-8")
        except OSError:
            pass
        return found

    def _put(self, directory: Path, files: dict[str, str]) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        os.chmod(directory, 0o755)
        for name, content in files.items():
            _write(directory / name, content)
        for path in directory.glob("*.conf"):
            if path.name not in files:
                path.unlink(missing_ok=True)

    def check(self, person: PersonSet) -> None:
        """keyd's own parse of a prepared set, or KeymapError."""
        check_dir = self.system_dir / "check"
        files = self._files_for(person)
        self._put(check_dir, files)
        checked = {str(check_dir / (LIVE_PREFIX + p.name)): p for p in person.files.values()}
        try:
            result = self.run(["keyd", "check", *sorted(checked)], capture_output=True,
                              text=True, timeout=10, **CHECK_AS)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise KeymapError(f"keyd could not check the files: {e}") from e
        finally:
            self._put(check_dir, {})
        if result.returncode != 0:
            problems = explain_check((result.stdout or "") + (result.stderr or ""), checked)
            raise KeymapError("; ".join(problems) if problems
                              else "keyd could not read the files")

    # -- the commands -------------------------------------------------------

    def _write_commands(self, uid: int) -> None:
        person = self.configs.get(uid)
        path = self.commands_dir / f"{uid}.json"
        if person is None or not person.commands.table:
            path.unlink(missing_ok=True)
            return
        self.commands_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.commands_dir, 0o700)
        _write(path, json.dumps({str(k): v for k, v in person.commands.table.items()}),
               mode=0o600)

    # -- what callers do ----------------------------------------------------

    def set(self, uid: int, files: dict[str, str], active_uid: int | None) -> bool:
        """A person's files, as they saved them. No files means none.

        Files that fail change nothing: whatever of theirs was in force
        stays in force. Returns whether keyd was reloaded."""
        files = {name: text for name, text in files.items() if text.strip()}
        if not files:
            self.configs.pop(uid, None)
        else:
            person = prepare_set(files, uid=uid, data_dir=self.data_dir)
            self.check(person)
            self.configs[uid] = person
        self._write_commands(uid)
        return self.refresh(active_uid)

    def bind(self, uid: int, bindings: list[str], active_uid: int | None) -> None:
        """app.conf's bindings for the window now in front, on top of the
        person's files. Only for the person on the screen."""
        if uid != active_uid:
            raise KeymapError("only the person using the screen can change its keys")
        self.refresh(active_uid)
        person = self.configs.get(uid)
        if person is None:
            person = self.configs[uid] = PersonSet({}, Commands())
        before = dict(person.commands.table)
        exprs = prepare_bindings(bindings, uid=uid, commands=person.commands)
        if person.commands.table != before:
            self._write_commands(uid)
        result = self._keyd("bind", "reset", *exprs)
        self.bound_uid = uid
        if result is not None and result.returncode != 0:
            raise KeymapError("keyd did not take every binding: "
                              + (result.stderr or result.stdout or "").strip())

    def forget_all_but(self, signed_in: set[int]) -> None:
        """Drop the sets of people with no session left."""
        for uid in list(self.configs):
            if uid not in signed_in:
                del self.configs[uid]
                self._write_commands(uid)

    def _keyd(self, *args: str):
        try:
            return self.run(["keyd", *args], capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.TimeoutExpired) as e:
            log.warning("keyboard: keyd %s failed: %s", args[0], e)
            return None

    def refresh(self, active_uid: int | None) -> bool:
        """Put the right set in force. Returns whether keyd was reloaded."""
        wanted = self.wanted(active_uid)
        if self.live() == wanted:
            if self.bound_uid is not None and self.bound_uid != active_uid:
                # Same files, but someone else's app bindings on top.
                self._keyd("bind", "reset")
                self.bound_uid = None
            return False
        self._put(self.system_dir, wanted)
        result = self._keyd("reload")
        self.bound_uid = None
        if result is None or result.returncode != 0:
            if result is not None:
                log.warning("keyboard: keyd reload failed: %s",
                            (result.stderr or result.stdout or "").strip())
            return False
        log.info("keyboard: in force for %s",
                 "uid %d" % active_uid if active_uid in self.configs else "nobody")
        return True


# ---- kosher-keyd-run: a command() key, run as its owner -----------------------

_NUMBER = re.compile(r"[0-9]{1,10}")


def read_command(commands_dir: Path, uid: int, number: str) -> str | None:
    """The command kosherd stored for this person and number, from a file
    only root could have written; None if there is none."""
    path = commands_dir / f"{uid}.json"
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return None
    with os.fdopen(fd, encoding="utf-8") as f:
        st = os.fstat(f.fileno())
        if st.st_uid != 0 or st.st_mode & 0o022:
            return None
        try:
            table = json.load(f)
        except ValueError:
            return None
    body = table.get(number) if isinstance(table, dict) else None
    return body if isinstance(body, str) else None


def active_seat_uid(run=subprocess.run) -> int | None:
    """Whose session is on seat0's screen, from loginctl; None for the
    login screen or anything else that is not a person."""
    def show(*args):
        r = run(["loginctl", *args], capture_output=True, text=True, timeout=5)
        return dict(line.split("=", 1) for line in r.stdout.splitlines() if "=" in line) \
            if r.returncode == 0 else {}
    session = show("show-seat", "seat0", "-p", "ActiveSession").get("ActiveSession", "")
    if not session:
        return None
    props = show("show-session", session, "-p", "User", "-p", "Class")
    if props.get("Class") != "user" or not props.get("User", "").isdigit():
        return None
    return int(props["User"])


def rate_ok(commands_dir: Path, uid: int, now: float | None = None) -> bool:
    """At most one command a MIN_COMMAND_INTERVAL for each person."""
    import fcntl

    now = time.time() if now is None else now
    stamp = commands_dir / f"{uid}.last"
    fd = os.open(stamp, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "r+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            last = float(f.read() or 0)
        except ValueError:
            last = 0.0
        if 0 <= now - last < MIN_COMMAND_INTERVAL:
            return False
        f.seek(0)
        f.truncate()
        f.write(repr(now))
    return True


def launch_argv(number: str, body: str, now_ns: int) -> list[str]:
    """systemd-run in the person's own manager: the command gets their
    session's environment (WAYLAND_DISPLAY and the rest, which the session
    hands its manager) and runs as a unit of theirs."""
    return ["systemd-run", "--user", "--collect", "--quiet",
            f"--unit=kosher-key-{number}-{now_ns}", "--", "/bin/sh", "-c", body]


def decide(argv: list[str], *, commands_dir: Path, active=active_seat_uid,
           rate=rate_ok, read=read_command) -> tuple[int, str, str] | None:
    """(uid, number, command) to run for a key, or None to do nothing."""
    if len(argv) != 2 or not all(_NUMBER.fullmatch(a) for a in argv):
        return None
    uid, number = int(argv[0]), argv[1]
    if uid < 1000:
        return None
    body = read(commands_dir, uid, number)
    if body is None:
        return None
    if active() != uid:
        # The key belongs to someone whose session is not on the screen
        # (or it is the login screen): it does nothing.
        return None
    if not rate(commands_dir, uid):
        return None
    return uid, number, body


def run_main(argv=None) -> int:  # pragma: no cover - runs as root, from keyd
    """kosher-keyd-run <uid> <n>: what keyd runs for a command() key."""
    import pwd
    import sys

    found = decide(list(sys.argv[1:] if argv is None else argv), commands_dir=COMMANDS_DIR)
    if found is None:
        return 1
    uid, number, body = found
    pw = pwd.getpwuid(uid)
    env = {"HOME": pw.pw_dir, "USER": pw.pw_name, "LOGNAME": pw.pw_name,
           "PATH": "/usr/local/bin:/usr/bin:/bin",
           "XDG_RUNTIME_DIR": f"/run/user/{uid}",
           "DBUS_SESSION_BUS_ADDRESS": f"unix:path=/run/user/{uid}/bus"}
    subprocess.run(launch_argv(number, body, time.time_ns()), env=env, cwd="/",
                   user=uid, group=pw.pw_gid,
                   extra_groups=os.getgrouplist(pw.pw_name, pw.pw_gid),
                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL, timeout=10, check=False)
    return 0


# ---- in the person's session ------------------------------------------------

def _dbus_message(error) -> str:
    """What kosherd said, without the D-Bus error name in front.

    Not Gio.DBusError.strip_remote_error: it returns a bool, and under
    PyGObject it strips a copy, so the watcher once reported "not applied:
    True" for a refused file.
    """
    message = error.message
    if message.startswith("GDBus.Error:"):
        _name, sep, rest = message.partition(": ")
        if sep:
            return rest
    return message


def read_own(home: Path) -> dict[str, str]:
    """The person's keyboard files by name (app.conf is not one)."""
    folder = home / USER_DIR
    files = {}
    try:
        paths = sorted(folder.glob("*.conf"))
    except OSError:
        return files
    for path in paths:
        if path.name == APP_CONF or not path.is_file():
            continue
        try:
            files[path.name] = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as e:
            raise KeymapError(f"could not read {path.name}: {e}") from e
    return files


def parse_devices(text: str) -> list[tuple[str, str, str]]:
    """(id, kind, name) for each keyboard and mouse in /proc/bus/input/devices,
    the same vendor:product keyd's ids start with. keyd's own virtual
    devices are left out."""
    found = []
    for block in text.strip().split("\n\n"):
        info = {}
        for line in block.splitlines():
            tag, _, rest = line.partition(": ")
            info.setdefault(tag, rest)
        ident = dict(f.split("=", 1) for f in info.get("I", "").split() if "=" in f)
        vendor, product = ident.get("Vendor", ""), ident.get("Product", "")
        handlers = info.get("H", "").split("=", 1)[-1].split()
        kind = "keyboard" if "kbd" in handlers else "mouse" if "mouse0" in handlers or \
            any(h.startswith("mouse") for h in handlers) else ""
        if not kind or not vendor or vendor.lower() == "0fac":
            continue
        name = info.get("N", "").split("=", 1)[-1].strip('"')
        found.append((f"{vendor.lower()}:{product.lower()}", kind, name))
    return found


def parse_bind_argv(argv: list[str]) -> list[str] | None:
    """The bindings in a `keyd bind [reset] <binding>...` command line, as
    keyd-application-mapper runs it; None for anything else."""
    if not argv or argv[0] not in ("bind", "-e", "--expression"):
        return None
    return [a for a in argv[1:] if a != "reset"]


def bind_main(argv=None) -> int:  # pragma: no cover - needs kosherd
    """kosher-keyd-bind: keyd-application-mapper's KEYD_BIN. Takes the
    mapper's `bind reset ...` and hands the bindings to kosherd."""
    import sys

    import gi

    gi.require_version("Gio", "2.0")
    from gi.repository import Gio, GLib

    from . import BUS_NAME, OBJECT_PATH

    bindings = parse_bind_argv(list(sys.argv[1:] if argv is None else argv))
    if bindings is None:
        print("kosher-keyd-bind only passes on `bind`; the rest of keyd is root's",
              file=sys.stderr)
        return 2
    try:
        Gio.bus_get_sync(Gio.BusType.SYSTEM, None).call_sync(
            BUS_NAME, OBJECT_PATH, "org.kosherlinux.Daemon1.Profiles", "SetMyAppKeys",
            GLib.Variant("(as)", (bindings,)), None, Gio.DBusCallFlags.NONE, 10000, None)
    except GLib.Error as e:
        print(_dbus_message(e), file=sys.stderr)
        return 1
    return 0


MAPPER = "/usr/libexec/kosher/keyd-application-mapper"
BIND_SHIM = "/usr/bin/kosher-keyd-bind"


def open_focus_pipe(runtime_dir: Path) -> int:
    """The pipe GNOME's keyd-focus extension writes the window in front
    to, made if missing, opened for reading AND writing. The mapper gets
    this descriptor across exec, so the pipe always has a writer: when the
    extension's end closes (GNOME Shell restarting), upstream's mapper
    would otherwise read end-of-file in a tight loop, asking for keys
    each time round."""
    import stat

    path = runtime_dir / "keyd.fifo"
    try:
        os.mkfifo(path, 0o600)
    except FileExistsError:
        pass
    fd = os.open(path, os.O_RDWR | os.O_NONBLOCK | os.O_NOFOLLOW)
    if not stat.S_ISFIFO(os.fstat(fd).st_mode):
        os.close(fd)
        raise OSError(f"{path} is not a pipe")
    os.set_inheritable(fd, True)
    return fd


def apps_main() -> int:  # pragma: no cover - execs the mapper
    """kosher-keyd-apps: upstream's keyd-application-mapper, with its
    `keyd bind` calls sent to kosherd (KEYD_BIN) and its pipe held open."""
    runtime = Path(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}")
    open_focus_pipe(runtime)
    os.environ["KEYD_BIN"] = BIND_SHIM
    os.execv(MAPPER, [MAPPER])
    return 1


def main(argv=None) -> int:  # pragma: no cover - needs a session and kosherd
    """kosher-keyboard: apply this account's keyd files.

    `kosher-keyboard` sends the files once and says what happened, for a
    person at a terminal. `kosher-keyboard --watch` is what the session
    runs: it sends them when kosherd appears (at sign-in, and again if
    kosherd restarts) and every time one is saved, shows a notification
    when keyd refuses them, and starts the app mapper when app.conf
    appears. `--list-devices` prints the keyboards' ids for [ids].
    """
    import argparse
    import sys

    p = argparse.ArgumentParser(prog="kosher-keyboard",
                                description="apply the files in ~/.config/keyd")
    p.add_argument("--watch", action="store_true",
                   help="keep running, and apply the files each time one is saved")
    p.add_argument("--list-devices", action="store_true",
                   help="print each keyboard and mouse with the id [ids] takes")
    args = p.parse_args(argv)
    if args.list_devices:
        devices = parse_devices(Path("/proc/bus/input/devices").read_text())
        for ident, kind, name in devices:
            print(f"{ident}  {kind:<8}  {name}")
        return 0

    import gi

    gi.require_version("Gio", "2.0")
    from gi.repository import Gio, GLib

    from . import BUS_NAME, OBJECT_PATH

    logging.basicConfig(level=logging.INFO, format="%(name)s: %(message)s",
                        stream=sys.stderr)
    home = Path.home()
    system = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)

    def send() -> str | None:
        """Apply the files; None when it worked, else what went wrong."""
        try:
            files = read_own(home)
            system.call_sync(BUS_NAME, OBJECT_PATH, "org.kosherlinux.Daemon1.Profiles",
                             "SetMyKeyboard", GLib.Variant("(a{ss})", (files,)), None,
                             Gio.DBusCallFlags.NONE, 30000, None)
        except KeymapError as e:
            return str(e)
        except GLib.Error as e:
            return _dbus_message(e)
        return None

    if not args.watch:
        problem = send()
        if problem:
            print(f"Not applied: {problem}", file=sys.stderr)
            return 1
        names = sorted(read_own(home))
        print(f"Applied {', '.join(names)}." if names
              else f"There are no .conf files in ~/{USER_DIR}, so nothing is remapped.")
        return 0

    session = Gio.bus_get_sync(Gio.BusType.SESSION, None)

    def notify(problem: str) -> None:
        try:
            session.call_sync(
                "org.freedesktop.Notifications", "/org/freedesktop/Notifications",
                "org.freedesktop.Notifications", "Notify",
                GLib.Variant("(susssasa{sv}i)", (
                    "KosherOS", 0, "input-keyboard-symbolic",
                    "Your keyboard files were not applied",
                    f"{problem}\n\nThe keys work as they did before you saved.",
                    [], {}, 20000)),
                GLib.VariantType("(u)"), Gio.DBusCallFlags.NONE, 5000, None)
        except GLib.Error as e:
            log.warning("could not show the notification: %s", e.message)

    def start_mapper() -> None:
        # Starting a running unit does nothing, and the unit's own
        # condition skips it when app.conf has gone.
        subprocess.run(["systemctl", "--user", "--no-block", "start",
                        "kosher-keyd-apps.service"], check=False)

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

    folder = Gio.File.new_for_path(str(home / USER_DIR))
    try:
        folder.make_directory_with_parents(None)
    except GLib.Error:
        pass
    monitor = folder.monitor_directory(Gio.FileMonitorFlags.WATCH_MOVES, None)

    def changed(_monitor, child, other, _event) -> None:
        names = {f.get_basename() for f in (child, other) if f is not None}
        if APP_CONF in names and (home / USER_DIR / APP_CONF).is_file():
            start_mapper()
        if any(n.endswith(".conf") and n != APP_CONF for n in names):
            soon()

    monitor.connect("changed", changed)
    Gio.bus_watch_name(Gio.BusType.SYSTEM, BUS_NAME, Gio.BusNameWatcherFlags.NONE,
                       lambda *_a: soon(), None)
    GLib.MainLoop().run()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
