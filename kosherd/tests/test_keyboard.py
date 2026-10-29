"""Per-person key remapping through keyd (kosherd/keyboard.py).

The parts that matter for safety are pure and tested here: what a
person's files may contain before keyd sees them, how a command() key
becomes a call to the helper and what the helper agrees to run, how
keyd's complaints map back to their files and lines, and whose config is
in force as sessions come and go. keyd itself is faked by a recorder; set
KOSHER_TEST_KEYD to a keyd binary to also run its own parser over the
prepared files.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from kosherd import access, keyboard, layout
from kosherd.daemon import INTROSPECTION_XML, Daemon
from kosherd.keyboard import (HEADER, HEADER_LINES, LIVE_PREFIX, RUN_HELPER, Commands,
                              Keyboards, KeymapError, prepare, prepare_bindings,
                              prepare_set)

ROOT = Path(__file__).parents[2]
FILES = ROOT / "os-image/files"
CONTAINERFILE = (ROOT / "os-image/Containerfile").read_text()
BASE = FILES / "etc/keyd/default.conf"
USER_UNIT = FILES / "usr/lib/systemd/user/kosher-keyboard.service"
APPS_UNIT = FILES / "usr/lib/systemd/user/kosher-keyd-apps.service"
KEYD_DROPIN = FILES / "usr/lib/systemd/system/keyd.service.d/10-kosheros.conf"
RUN_WRAPPER = FILES / "usr/libexec/kosher-keyd-run"
EXTENSION = FILES / "usr/share/gnome-shell/extensions/keyd-focus@kosherlinux.org"

CAPS = "[main]\ncapslock = overload(control, esc)\n"
PAD = "[ids]\n046d:c52b\n\n[main]\na = b\n"


@pytest.fixture
def data_dir(tmp_path):
    d = tmp_path / "share-keyd"
    (d / "layouts").mkdir(parents=True)
    (d / "layouts/colemak").write_text("a = a\n")
    (tmp_path / "secret").write_text("root's business\n")
    return d


def prep(text, data_dir, **kw):
    return prepare(text, data_dir=data_dir, **kw).text


# -- one file -----------------------------------------------------------------

def test_a_file_without_ids_is_for_every_keyboard_and_keeps_its_lines(data_dir):
    p = prepare(CAPS, data_dir=data_dir)
    assert p.text == HEADER + CAPS and p.wildcard and p.offset == HEADER_LINES
    assert p.text.splitlines()[HEADER_LINES:] == CAPS.splitlines()


def test_their_own_ids_are_kept(data_dir):
    p = prepare("[ids]\n046D:C52B\nm:1234:abcd:0f0f0f0f\n\n[main]\na = b\n",
                data_dir=data_dir)
    assert p.offset == 0 and not p.wildcard
    assert p.text.splitlines()[:3] == ["[ids]", "046d:c52b", "m:1234:abcd:0f0f0f0f"]
    assert p.ids == ["046d:c52b", "1234:abcd:0f0f0f0f"]


def test_a_wildcard_with_exclusions(data_dir):
    p = prepare("[ids]\n*\n-046d:c52b\n[main]\na = b\n", data_dir=data_dir)
    assert p.wildcard and p.ids == []


@pytest.mark.parametrize("entry", [
    "046d", "046d:c52", "046d:c52bx", "x:046d:c52b", "-*", "--046d:c52b",
    "046d:c52b = a", "*046d", "../../etc", "046d:c52b:123456789",
])
def test_ids_take_only_keyds_own_id_syntax(data_dir, entry):
    with pytest.raises(KeymapError, match="not a device id"):
        prepare(f"[ids]\n{entry}\n[main]\na = b\n", data_dir=data_dir)


def test_one_ids_section_and_no_include_in_it(data_dir):
    with pytest.raises(KeymapError, match="second"):
        prepare("[ids]\n*\n[main]\n[ids]\n046d:c52b\n", data_dir=data_dir)
    with pytest.raises(KeymapError, match="include"):
        prepare("[ids]\ninclude layouts/colemak\n", data_dir=data_dir)


def test_too_many_ids(data_dir):
    ids = "".join(f"{i:04x}:0001\n" for i in range(keyboard.MAX_IDS + 1))
    with pytest.raises(KeymapError, match="devices"):
        prepare("[ids]\n" + ids, data_dir=data_dir)


def test_includes_reach_only_what_keyd_ships(data_dir):
    ok = "[main]\ninclude layouts/colemak\n"
    assert prep(ok, data_dir) == HEADER + ok
    for bad in ("../secret", "/etc/shadow", "layouts/../../secret", "layouts/missing",
                "layouts", "check/person-default.conf", "default.conf"):
        with pytest.raises(KeymapError, match="include"):
            prepare(f"[main]\ninclude {bad}\n", data_dir=data_dir)


def test_an_include_cannot_escape_through_a_link(data_dir):
    (data_dir / "layouts/sneaky").symlink_to(data_dir.parent / "secret")
    with pytest.raises(KeymapError, match="include"):
        prepare("[main]\ninclude layouts/sneaky\n", data_dir=data_dir)


@pytest.mark.parametrize("text,match", [
    ("#" * (keyboard.MAX_BYTES + 1), "larger"),
    ("\n" * (keyboard.MAX_LINES + 1), "lines"),
    ("[main]\na = " + "b" * keyboard.MAX_LINE + "\n", "longer"),
    ("".join(f"[l{i}]\n" for i in range(keyboard.MAX_SECTIONS + 1)), "sections"),
    ("[main]\n" + "include layouts/colemak\n" * (keyboard.MAX_INCLUDES + 1), "includes"),
    ("[main]\na = b\x00\n", "control"),
    ("[main]\na = \x1b[31m\n", "control"),
])
def test_what_would_trip_keyds_own_limits_is_refused_first(data_dir, text, match):
    # keyd checks its limits with assert(), so a file past them would
    # abort the daemon instead of being refused.
    with pytest.raises(KeymapError, match=match):
        prepare(text, data_dir=data_dir)


def test_crlf_and_tabs_and_unicode_are_fine(data_dir):
    out = prep("[main]\r\na\t= ä\r\n", data_dir)
    assert out.splitlines()[HEADER_LINES:] == ["[main]", "a\t= ä"]


def test_the_computers_own_config_remaps_nothing():
    lines = [ln.strip() for ln in BASE.read_text().splitlines()
             if ln.strip() and not ln.strip().startswith("#")]
    assert lines == ["[ids]", "*", "[main]"]


# -- command(): the person's, never root's -------------------------------------

def test_a_command_key_becomes_the_helper_with_two_numbers(data_dir):
    commands = Commands()
    p = prepare("[main]\nf5 = command(notify-send 'hi; there' $(id))\n"
                "[nav]\nh = command(  brightnessctl set 10%-  )\n"
                "j = command(notify-send 'hi; there' $(id))\n",
                uid=1001, commands=commands, data_dir=data_dir)
    lines = p.text.splitlines()[HEADER_LINES:]
    assert lines[1] == f"f5 = command({RUN_HELPER} 1001 0)"
    assert lines[3] == f"h = command({RUN_HELPER} 1001 1)"
    assert lines[4] == f"j = command({RUN_HELPER} 1001 0)", "the same command, the same number"
    assert commands.table == {0: "notify-send 'hi; there' $(id)", 1: "brightnessctl set 10%-"}
    assert "notify-send" not in p.text and "brightnessctl" not in p.text, \
        "nothing of the person's text reaches keyd"


@pytest.mark.parametrize("line", [
    "capslock = overload(control, command(id))",
    "a = macro(command(id))",
    "a = command (id)",
    "a = COMMAND(id)",
    "command(id) = a",
    "a = timeout(b, 200, command(id))",
])
def test_command_anywhere_but_a_keys_whole_action_is_refused(data_dir, line):
    with pytest.raises(KeymapError, match="command"):
        prepare("[main]\n" + line + "\n", data_dir=data_dir)


def test_commands_have_limits(data_dir):
    with pytest.raises(KeymapError, match="empty"):
        prepare("[main]\na = command( )\n", data_dir=data_dir)
    with pytest.raises(KeymapError, match="longer"):
        prepare("[main]\na = command(" + "x" * (keyboard.MAX_COMMAND + 1) + ")\n",
                data_dir=data_dir)
    many = "".join(f"k{i} = command(echo {i})\n" for i in range(keyboard.MAX_FILE_COMMANDS + 1))
    with pytest.raises(KeymapError, match="command"):
        prepare("[main]\n" + many, data_dir=data_dir)


def test_a_comment_may_mention_command(data_dir):
    text = "# command(...) runs as you\n[main]\na = b\n"
    assert prep(text, data_dir) == HEADER + text


def test_the_error_names_the_file_and_line(data_dir):
    with pytest.raises(KeymapError, match=r"^pad.conf line 3:"):
        prepare("[main]\na = b\nc = overload(x, command(id))\n", name="pad.conf",
                data_dir=data_dir)


# -- the helper keyd runs --------------------------------------------------------

@pytest.fixture
def cmd_dir(tmp_path):
    d = tmp_path / "kosher-keyd"
    d.mkdir(mode=0o700)
    return d


def store(cmd_dir, uid, table, mode=0o600):
    path = cmd_dir / f"{uid}.json"
    path.write_text(json.dumps(table))
    os.chmod(path, mode)
    return path


def decide(argv, cmd_dir, active=1001, rate=True):
    # The files here are the test's own, not root's, so they are read
    # without the ownership check, which is tested on its own below.
    def read(d, uid, n):
        path = d / f"{uid}.json"
        return json.loads(path.read_text()).get(n) if path.exists() else None
    return keyboard.decide(argv, commands_dir=cmd_dir, active=lambda: active,
                           rate=lambda d, u: rate, read=read)


def test_the_helper_runs_the_stored_command_for_the_person_on_the_screen(cmd_dir):
    store(cmd_dir, 1001, {"0": "notify-send hi"})
    assert decide(["1001", "0"], cmd_dir) == (1001, "0", "notify-send hi")


@pytest.mark.parametrize("argv", [
    [], ["1001"], ["1001", "0", "x"], ["1001", "0; id"], ["1001", "-1"], ["10o1", "0"],
    ["1001", "0 "], ["0", "0"], ["999", "0"], ["1001", "7"], ["1002", "0"],
])
def test_the_helper_takes_two_plain_numbers_it_knows(cmd_dir, argv):
    store(cmd_dir, 1001, {"0": "notify-send hi"})
    assert decide(argv, cmd_dir) is None


def test_the_helper_does_nothing_for_someone_not_on_the_screen(cmd_dir):
    store(cmd_dir, 1001, {"0": "notify-send hi"})
    assert decide(["1001", "0"], cmd_dir, active=1002) is None
    assert decide(["1001", "0"], cmd_dir, active=None) is None, "the login screen"


def test_the_helper_holds_back_a_burst(cmd_dir):
    store(cmd_dir, 1001, {"0": "x"})
    assert decide(["1001", "0"], cmd_dir, rate=False) is None
    assert keyboard.rate_ok(cmd_dir, 1001, now=100.0)
    assert not keyboard.rate_ok(cmd_dir, 1001, now=100.05)
    assert keyboard.rate_ok(cmd_dir, 1001, now=100.3)
    assert keyboard.rate_ok(cmd_dir, 1002, now=100.31), "each person on their own"


def test_the_helper_reads_only_what_root_wrote(cmd_dir):
    store(cmd_dir, 1001, {"0": "x"})
    # Owned by the test's user, not root: refused, as an account's own
    # file would be.
    if os.getuid() != 0:
        assert keyboard.read_command(cmd_dir, 1001, "0") is None
    link = cmd_dir / "1002.json"
    link.symlink_to(cmd_dir / "1001.json")
    assert keyboard.read_command(cmd_dir, 1002, "0") is None, "no links"


def test_the_command_runs_through_the_persons_own_manager():
    argv = keyboard.launch_argv("3", "notify-send 'a b'", 42)
    assert argv[:2] == ["systemd-run", "--user"]
    assert argv[-3:] == ["/bin/sh", "-c", "notify-send 'a b'"], \
        "the shell is the person's, given the text as one argument"
    assert "--unit=kosher-key-3-42" in argv


def test_whose_session_is_on_the_screen_comes_from_logind():
    def loginctl(answers):
        def run(argv, **kw):
            return SimpleNamespace(returncode=0, stdout=answers[argv[1]])
        return run
    assert keyboard.active_seat_uid(loginctl({
        "show-seat": "ActiveSession=4\n", "show-session": "User=1001\nClass=user\n"})) == 1001
    assert keyboard.active_seat_uid(loginctl({
        "show-seat": "ActiveSession=c1\n", "show-session": "User=42\nClass=greeter\n"})) is None
    assert keyboard.active_seat_uid(loginctl({
        "show-seat": "ActiveSession=\n", "show-session": ""})) is None


def test_the_helper_is_installed_as_keyd_names_it():
    assert RUN_WRAPPER.read_text().startswith("#!/usr/bin/python3 -I\n")
    assert "run_main" in RUN_WRAPPER.read_text()
    assert os.access(RUN_WRAPPER, os.X_OK)
    assert str(RUN_WRAPPER).endswith(RUN_HELPER)
    assert "chmod 755 /usr/libexec/kosher-keyd-run" in CONTAINERFILE


# -- a set of files, one per keyboard -------------------------------------------

def test_a_set_with_a_default_and_a_keyboard_of_its_own(data_dir):
    s = prepare_set({"default.conf": CAPS, "pad.conf": PAD}, data_dir=data_dir)
    assert s.wildcard and set(s.files) == {"default.conf", "pad.conf"}


def test_two_files_for_every_keyboard_are_refused(data_dir):
    with pytest.raises(KeymapError, match="default.conf and laptop.conf both apply to every"):
        prepare_set({"default.conf": CAPS, "laptop.conf": "[ids]\n*\n[main]\n"},
                    data_dir=data_dir)


@pytest.mark.parametrize("other", ["046d:c52b", "046d:c52b:0f0f0f0f", "k:046d:c52b"])
def test_one_keyboard_cannot_have_two_files(data_dir, other):
    # keyd picks between them by directory order, which is no order at all.
    with pytest.raises(KeymapError, match="both name"):
        prepare_set({"pad.conf": PAD, "pad2.conf": f"[ids]\n{other}\n[main]\n"},
                    data_dir=data_dir)


@pytest.mark.parametrize("name", ["app.conf", "x.txt", ".conf", "../x.conf", "a b.conf",
                                  "x" * 40 + ".conf", "default.conf.bak"])
def test_file_names(data_dir, name):
    with pytest.raises(KeymapError, match="name"):
        prepare_set({name: CAPS}, data_dir=data_dir)


def test_too_many_files(data_dir):
    files = {f"k{i}.conf": f"[ids]\n{i:04x}:0001\n" for i in range(keyboard.MAX_FILES + 1)}
    with pytest.raises(KeymapError, match="files"):
        prepare_set(files, data_dir=data_dir)


def test_commands_are_numbered_across_the_set(data_dir):
    s = prepare_set({"default.conf": "[main]\na = command(one)\n",
                     "pad.conf": PAD + "b = command(two)\n"}, uid=1001, data_dir=data_dir)
    assert s.commands.table == {0: "one", 1: "two"}


# -- keyd's complaints, in the person's files and lines ---------------------------

def test_keyd_check_output_maps_back_to_their_lines(data_dir):
    d = prepare(CAPS, name="default.conf", data_dir=data_dir)
    p = prepare(PAD, name="pad.conf", data_dir=data_dir)
    out = ("Parsing /run/keyd/check/person-default.conf\n"
           "\tWARNING: /run/keyd/check/person-default.conf:8: invalid key or action\n"
           "\tWARNING: /run/keyd/check/person-pad.conf:5: invalid key or action\n"
           "\tWARNING: /usr/share/keyd/layouts/de:3: invalid key or action\n")
    assert keyboard.explain_check(out, {"/run/keyd/check/person-default.conf": d,
                                        "/run/keyd/check/person-pad.conf": p}) == [
        "default.conf line 6: invalid key or action",
        "pad.conf line 5: invalid key or action",
        "/usr/share/keyd/layouts/de line 3: invalid key or action",
    ]


# -- whose config is in force ---------------------------------------------------

class FakeKeyd:
    """Records what kosherd asks keyd for; `check_output` makes check fail."""

    def __init__(self):
        self.calls = []
        self.check_output = None
        self.checked_as = None
        self.checked = []
        self.bind_rc = 0

    def __call__(self, argv, **kwargs):
        self.calls.append(argv[1:])
        if argv[1] == "check":
            self.checked_as = {k: kwargs.get(k) for k in ("user", "group", "extra_groups")}
            self.checked = [Path(a).name for a in argv[2:]]
            if self.check_output:
                return SimpleNamespace(returncode=255, stdout=self.check_output.replace(
                    "PATH", argv[2]), stderr="")
        if argv[1] == "bind":
            return SimpleNamespace(returncode=self.bind_rc, stdout="", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    @property
    def reloads(self):
        return sum(1 for c in self.calls if c == ["reload"])


@pytest.fixture
def kb(tmp_path, data_dir):
    run = FakeKeyd()
    k = Keyboards(system_dir=tmp_path / "run-keyd", base=BASE, data_dir=data_dir,
                  commands_dir=tmp_path / "kosher-keyd", run=run)
    k.fake = run
    return k


def live(kb):
    return kb.live()


BASE_ONLY = {"default.conf": BASE.read_text()}


def test_a_person_on_the_screen_gets_theirs_and_not_the_machines(kb):
    assert kb.set(1001, {"default.conf": CAPS}, active_uid=1001)
    assert live(kb) == {LIVE_PREFIX + "default.conf": HEADER + CAPS}, \
        "their file is for every keyboard, so the machine's is not loaded beside it"
    assert kb.fake.calls == [["check", str(kb.system_dir / "check/person-default.conf")],
                             ["reload"]]
    assert not list((kb.system_dir / "check").glob("*.conf")), "the candidates are cleaned up"


def test_keyboards_their_files_do_not_name_keep_the_machines(kb):
    kb.set(1001, {"pad.conf": PAD}, active_uid=1001)
    assert live(kb) == {"default.conf": BASE.read_text(), LIVE_PREFIX + "pad.conf": PAD}


def test_their_names_never_replace_the_machines_file(kb):
    kb.set(1001, {"default.conf": PAD}, active_uid=1001)
    assert live(kb)["default.conf"] == BASE.read_text()
    assert live(kb)[LIVE_PREFIX + "default.conf"] == PAD


def test_the_whole_set_is_checked_together_as_nobody(kb):
    kb.set(1001, {"default.conf": CAPS, "pad.conf": PAD}, active_uid=1001)
    assert kb.fake.checked == ["person-default.conf", "person-pad.conf"]
    assert kb.fake.checked_as == {"user": "nobody", "group": "nobody", "extra_groups": []}


def test_switching_people_switches_sets_and_the_login_screen_gets_the_machines(kb):
    kb.set(1001, {"default.conf": CAPS}, active_uid=1001)
    kb.set(1002, {"pad.conf": PAD}, active_uid=1001)
    assert live(kb) == {LIVE_PREFIX + "default.conf": HEADER + CAPS}, \
        "1002 is signed in but not on the screen"
    kb.refresh(1002)
    assert live(kb) == {"default.conf": BASE.read_text(), LIVE_PREFIX + "pad.conf": PAD}
    kb.refresh(None)
    assert live(kb) == BASE_ONLY, "the login screen"
    kb.refresh(1003)
    assert live(kb) == BASE_ONLY, "someone with no files"


def test_nothing_reloads_when_nothing_changed(kb):
    kb.set(1001, {"default.conf": CAPS}, active_uid=1001)
    reloads = kb.fake.reloads
    assert not kb.refresh(1001)
    kb.set(1001, {"default.conf": CAPS}, active_uid=1001)
    assert kb.fake.reloads == reloads


def test_a_keyd_restart_is_noticed(kb):
    kb.set(1001, {"default.conf": CAPS}, active_uid=1001)
    # keyd.service put the computer's config back, alone, as it restarted.
    for path in kb.system_dir.glob("*.conf"):
        path.unlink()
    (kb.system_dir / "default.conf").write_text(BASE.read_text())
    assert kb.refresh(1001)
    assert live(kb) == {LIVE_PREFIX + "default.conf": HEADER + CAPS}


def test_a_refused_set_leaves_what_was_in_force(kb):
    kb.set(1001, {"default.conf": CAPS}, active_uid=1001)
    kb.fake.check_output = "\tWARNING: PATH:5: invalid key or action\n"
    with pytest.raises(KeymapError, match="default.conf line 3: invalid key or action"):
        kb.set(1001, {"default.conf": "[main]\na = b\nfoo = bar\n", "pad.conf": PAD},
               active_uid=1001)
    assert live(kb) == {LIVE_PREFIX + "default.conf": HEADER + CAPS}
    kb.fake.check_output = None
    with pytest.raises(KeymapError, match="pad.conf"):
        kb.set(1001, {"default.conf": CAPS, "pad.conf": "[ids]\nnope\n"}, active_uid=1001)
    assert live(kb) == {LIVE_PREFIX + "default.conf": HEADER + CAPS}


def test_no_files_is_no_remaps(kb):
    kb.set(1001, {"default.conf": CAPS}, active_uid=1001)
    kb.set(1001, {"default.conf": "  \n"}, active_uid=1001)
    assert 1001 not in kb.configs and live(kb) == BASE_ONLY


def test_commands_are_stored_for_the_helper_and_dropped_at_sign_out(kb):
    kb.set(1001, {"default.conf": "[main]\nf5 = command(notify-send hi)\n"}, active_uid=1001)
    path = kb.commands_dir / "1001.json"
    assert json.loads(path.read_text()) == {"0": "notify-send hi"}
    assert path.stat().st_mode & 0o777 == 0o600
    assert kb.commands_dir.stat().st_mode & 0o777 == 0o700
    kb.forget_all_but(set())
    assert not path.exists() and kb.configs == {}


def test_a_keyd_that_will_not_reload_is_not_fatal(kb):
    kb.run = lambda argv, **kw: SimpleNamespace(returncode=1, stdout="", stderr="no socket")
    assert kb.refresh(None) is False, "logged, not raised"
    assert live(kb) == BASE_ONLY


# -- per-app keys (app.conf, through kosher-keyd-bind) -----------------------------

def test_app_bindings_go_to_keyd_checked_and_only_for_the_person_on_the_screen(kb):
    kb.set(1001, {"default.conf": CAPS}, active_uid=1001)
    kb.bind(1001, ["control.w = C-backspace", "f5 = command(make)"], active_uid=1001)
    assert kb.fake.calls[-1] == ["bind", "reset", "control.w = C-backspace",
                                 f"f5 = command({RUN_HELPER} 1001 0)"]
    assert json.loads((kb.commands_dir / "1001.json").read_text()) == {"0": "make"}
    with pytest.raises(KeymapError, match="screen"):
        kb.bind(1002, ["a = b"], active_uid=1001)
    with pytest.raises(KeymapError, match="command"):
        kb.bind(1001, ["a = overload(x, command(id))"], active_uid=1001)


def test_app_bindings_are_cleared_when_someone_else_takes_the_screen(kb):
    kb.bind(1001, ["a = b"], active_uid=1001)
    calls = len(kb.fake.calls)
    kb.refresh(1002)  # 1002 has no files: the same files are loaded
    assert kb.fake.calls[calls:] == [["bind", "reset"]]
    assert kb.bound_uid is None
    kb.refresh(1002)
    assert len(kb.fake.calls) == calls + 1, "and only once"


def test_the_same_app_bindings_are_not_sent_again(kb):
    # The mapper asks on every focus change; keyd's bind is one request per
    # binding, so a repeat left a moment with the app's keys missing.
    kb.bind(1001, ["a = b"], active_uid=1001)
    calls = len(kb.fake.calls)
    kb.bind(1001, ["a = b"], active_uid=1001)
    assert len(kb.fake.calls) == calls, "nothing changed, nothing sent"
    kb.bind(1001, ["a = c"], active_uid=1001)
    assert kb.fake.calls[-1] == ["bind", "reset", "a = c"]
    # After someone else had the screen, they are sent again.
    kb.refresh(1002)
    kb.bind(1001, ["a = c"], active_uid=1001)
    assert kb.fake.calls[-1] == ["bind", "reset", "a = c"]


def test_bindings_keyd_refused_are_sent_again(kb):
    kb.fake.bind_rc = 1
    with pytest.raises(KeymapError):
        kb.bind(1001, ["a = b"], active_uid=1001)
    calls = len(kb.fake.calls)
    kb.fake.bind_rc = 0
    kb.bind(1001, ["a = b"], active_uid=1001)
    assert kb.fake.calls[-1] == ["bind", "reset", "a = b"] and len(kb.fake.calls) > calls


def test_app_bindings_are_checked(kb):
    commands = Commands()
    for bad in (["a"], ["a = b\nc = d"], ["a = " + "b" * keyboard.MAX_BINDING],
                ["a = b"] * (keyboard.MAX_BINDINGS + 1)):
        with pytest.raises(KeymapError):
            prepare_bindings(bad, uid=1001, commands=commands)


def test_the_shim_takes_the_mappers_command_line():
    assert keyboard.parse_bind_argv(["bind", "reset", "a = b", "c = d"]) == ["a = b", "c = d"]
    assert keyboard.parse_bind_argv(["bind", "reset"]) == []
    assert keyboard.parse_bind_argv(["reload"]) is None
    assert keyboard.parse_bind_argv(["input", "x"]) is None
    assert keyboard.parse_bind_argv([]) is None


PROC_DEVICES = """\
I: Bus=0011 Vendor=0001 Product=0001 Version=ab83
N: Name="AT Translated Set 2 keyboard"
P: Phys=isa0060/serio0/input0
H: Handlers=sysrq kbd leds event2
B: EV=120013

I: Bus=0003 Vendor=046D Product=C52B Version=0111
N: Name="Logitech USB Receiver Mouse"
H: Handlers=event5 mouse1
B: EV=17

I: Bus=0006 Vendor=0fac Product=0ade Version=0001
N: Name="keyd virtual keyboard"
H: Handlers=sysrq kbd event9
B: EV=120013

I: Bus=0019 Vendor=0000 Product=0001 Version=0000
N: Name="Power Button"
H: Handlers=event0
B: EV=3
"""


def test_listing_devices_reads_names_and_ids_only():
    assert keyboard.parse_devices(PROC_DEVICES) == [
        ("0001:0001", "keyboard", "AT Translated Set 2 keyboard"),
        ("046d:c52b", "mouse", "Logitech USB Receiver Mouse"),
    ]
    for ident, _kind, _name in keyboard.parse_devices(PROC_DEVICES):
        assert keyboard._ID_ENTRY.fullmatch(ident), "what it prints, [ids] takes"


def test_reading_their_own_files(tmp_path):
    d = tmp_path / ".config/keyd"
    d.mkdir(parents=True)
    (d / "default.conf").write_text(CAPS)
    (d / "pad.conf").write_text(PAD)
    (d / "app.conf").write_text("[firefox]\na = b\n")
    (d / "notes.txt").write_text("x")
    (d / "dir.conf").mkdir()
    assert keyboard.read_own(tmp_path) == {"default.conf": CAPS, "pad.conf": PAD}
    assert keyboard.read_own(tmp_path / "nobody") == {}


# -- the daemon methods ---------------------------------------------------------

def test_the_methods_are_yours_alone_and_need_no_admin():
    for method in ("SetMyKeyboard", "SetMyAppKeys"):
        assert access.ACTIONS[method] == access.ACTION_SET_OWN
        assert method in access.UID_AWARE, "the uid comes from the bus"
        assert method not in access.GUARDIAN_GATED
        assert f'<method name="{method}">' in INTROSPECTION_XML
    assert 'type="a{ss}" name="files"' in INTROSPECTION_XML


def test_the_daemon_applies_for_the_caller_while_they_are_on_the_screen(kb):
    daemon = Daemon.__new__(Daemon)
    daemon.connection = None
    daemon.keyboards = kb
    daemon._active_seat_uid = lambda: 1001
    daemon.impl_SetMyKeyboard({"default.conf": CAPS}, _uid=1001)
    assert live(kb) == {LIVE_PREFIX + "default.conf": HEADER + CAPS}
    daemon.impl_SetMyKeyboard({"default.conf": "[main]\nb = a\n"}, _uid=1002)
    assert live(kb) == {LIVE_PREFIX + "default.conf": HEADER + CAPS}, "1002 is not on the screen"
    with pytest.raises(ValueError):
        daemon.impl_SetMyKeyboard({"default.conf": "[main]\na = macro(command(id))\n"},
                                  _uid=1001)
    with pytest.raises(ValueError):
        daemon.impl_SetMyAppKeys(["a = b"], _uid=1002)
    daemon.impl_SetMyAppKeys(["a = b"], _uid=1001)
    assert kb.fake.calls[-1] == ["bind", "reset", "a = b"]


# -- the image ------------------------------------------------------------------

def test_keyd_is_built_from_a_pinned_checked_release_reading_run():
    assert "as keyd-build" in CONTAINERFILE
    assert "ARG KEYD_VERSION=2.6.0" in CONTAINERFILE
    assert "sha256sum -c -" in CONTAINERFILE.split("as keyd-build")[1].split("FROM ")[0]
    assert "make PREFIX=/usr CONFIG_DIR=/run/keyd" in CONTAINERFILE
    assert "make install PREFIX=/usr CONFIG_DIR=/run/keyd" in CONTAINERFILE, \
        "both, or the man page and the binary disagree"
    assert "COPY --from=keyd-build /out/usr/ /usr/" in CONTAINERFILE
    # Off on a new install; kosherd enables it with the switch (#230).
    assert "systemctl enable keyd.service" not in CONTAINERFILE
    assert "! systemctl is-enabled --quiet keyd.service" in CONTAINERFILE


def test_nobody_gets_raw_input_or_keyds_socket():
    # The whole design rests on this: an account that can read /dev/input
    # can read the guardian password, and one on keyd's socket is root.
    assert "! test -e /usr/bin/keyd-application-mapper" in CONTAINERFILE, \
        "the mapper is not on PATH, where it would try keyd's socket"
    assert 'test -z "$(getent group keyd | cut -d: -f4)"' in CONTAINERFILE
    src = ROOT / "kosherd/src/kosherd"
    for path in [ROOT / "os-image/Containerfile", *src.glob("*.py"), *FILES.rglob("*")]:
        if not path.is_file() or path.suffix in (".png", ".svg", ".jpg", ".onnx"):
            continue
        try:
            text = path.read_text()
        except UnicodeDecodeError:
            continue
        for group in ("keyd", "input", "uinput"):
            assert f"-aG {group} " not in text and f"-G {group} " not in text, path


def test_keyd_starts_from_the_computers_config_alone_and_comes_back():
    text = KEYD_DROPIN.read_text()
    assert "rm -f /run/keyd/*.conf && install -D -m 0644 /etc/keyd/default.conf " \
           "/run/keyd/default.conf" in text, "a person's files do not outlive a restart"
    assert "Restart=always" in text


def test_every_session_runs_the_helper():
    unit = USER_UNIT.read_text()
    assert "ExecStart=/usr/bin/kosher-keyboard --watch" in unit
    assert "WantedBy=graphical-session.target" in unit
    assert "ConditionUser=!@system" in unit
    assert "systemctl --global enable kosher-keyboard.service kosher-keyd-apps.service" \
        in CONTAINERFILE
    pyproject = (ROOT / "kosherd/pyproject.toml").read_text()
    assert 'kosher-keyboard = "kosherd.keyboard:main"' in pyproject
    assert 'kosher-keyd-bind = "kosherd.keyboard:bind_main"' in pyproject


def test_the_app_mapper_runs_for_people_with_an_app_conf_through_kosherd():
    unit = APPS_UNIT.read_text()
    assert "ConditionPathExists=%h/.config/keyd/app.conf" in unit
    assert "ExecStart=/usr/bin/kosher-keyd-apps" in unit
    pyproject = (ROOT / "kosherd/pyproject.toml").read_text()
    assert 'kosher-keyd-apps = "kosherd.keyboard:apps_main"' in pyproject
    assert keyboard.BIND_SHIM == "/usr/bin/kosher-keyd-bind"
    assert keyboard.MAPPER == "/usr/libexec/kosher/keyd-application-mapper"
    assert f"{keyboard.MAPPER}" in CONTAINERFILE
    assert "WantedBy=graphical-session.target" in unit, \
        "GNOME, niri, Umbriel, and Hyprland through kosher-hyprland-session.target"
    # The mapper's wlroots backend (niri, Hyprland, Umbriel) and its
    # KEYD_BIN are checked for in the build, since the mapper is upstream's.
    assert "grep -q zwlr_foreign_toplevel_manager_v1" in CONTAINERFILE
    assert "KEYD_BIN = os.environ.get('KEYD_BIN', 'keyd')" in CONTAINERFILE


def test_the_gnome_extension_reports_focus_and_nothing_else():
    meta = json.loads((EXTENSION / "metadata.json").read_text())
    assert meta["uuid"] == layout.EXT_KEYD_FOCUS == EXTENSION.name
    assert meta["shell-version"] == ["50"]
    js = (EXTENSION / "extension.js").read_text()
    assert "keyd.fifo" in js and "Gio.FileType.SPECIAL" in js, "only ever an existing pipe"
    for word in ("spawn", "pkill", "mkfifo", "subprocess"):
        assert word not in js, word
    for name in ("classic", "tiling"):
        enabled = dict(((s, k), v) for s, k, v in layout.PLANS[name])[
            ("org.gnome.shell", "enabled-extensions")]
        assert layout.EXT_KEYD_FOCUS in enabled


def test_keyd_is_credited():
    third = (ROOT / "THIRD-PARTY.md").read_text()
    assert "keyd" in third and "MIT" in third.split("keyd", 1)[1][:400]


# -- keyd's own parser, when one is at hand -------------------------------------

KEYD = os.environ.get("KOSHER_TEST_KEYD") or shutil.which("keyd")


@pytest.mark.skipif(not KEYD, reason="no keyd binary (set KOSHER_TEST_KEYD)")
def test_keyd_accepts_what_prepare_makes(tmp_path, data_dir):
    good = {
        "a.conf": "[main]\ncapslock = overload(control, esc)\nf5 = command(notify-send hi)\n\n"
                  "[global]\noverload_tap_timeout = 200\n\n[nav]\nh = left\n",
        "b.conf": "[ids]\nk:046d:c52b\n-0001:0001\n[main]\na = b\n",
    }
    paths = []
    for name, text in good.items():
        path = tmp_path / name
        path.write_text(prepare(text, uid=1001, data_dir=data_dir).text)
        paths.append(str(path))
    r = subprocess.run([KEYD, "check", *paths], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    p = prepare("[main]\na = b\nfoo = bar\n", data_dir=data_dir)
    path = tmp_path / "bad.conf"
    path.write_text(p.text)
    r = subprocess.run([KEYD, "check", str(path)], capture_output=True, text=True)
    assert r.returncode != 0
    assert keyboard.explain_check(r.stdout + r.stderr, {str(path): p}) == \
        ["default.conf line 3: invalid key or action"]


def test_the_focus_pipe_always_has_a_writer(tmp_path):
    # Upstream's mapper reads end-of-file in a tight loop once the pipe's
    # last writer closes; the descriptor it inherits keeps one open.
    fd = keyboard.open_focus_pipe(tmp_path)
    try:
        assert os.get_inheritable(fd)
        reader = os.open(tmp_path / "keyd.fifo", os.O_RDONLY | os.O_NONBLOCK)
        writer = os.open(tmp_path / "keyd.fifo", os.O_WRONLY)
        os.write(writer, b"firefox\tx\n")
        os.close(writer)  # the extension's end goes away
        assert os.read(reader, 100) == b"firefox\tx\n"
        with pytest.raises(BlockingIOError):
            os.read(reader, 100)  # waiting for more, not end-of-file
        os.close(reader)
    finally:
        os.close(fd)
    again = keyboard.open_focus_pipe(tmp_path)  # an existing pipe is reused
    os.close(again)
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "keyd.fifo").write_text("not a pipe")
    with pytest.raises(OSError, match="not a pipe"):
        keyboard.open_focus_pipe(plain)


def test_a_refusal_is_reported_by_its_text_not_a_bool():
    """strip_remote_error returns a bool; the watcher once logged
    "not applied: True" for a refused file (stack VM run, #169)."""
    gi = pytest.importorskip("gi")
    gi.require_version("Gio", "2.0")
    from gi.repository import Gio, GLib

    error = GLib.Error.new_literal(
        Gio.DBusError.quark(),
        "GDBus.Error:org.kosherlinux.Error: default.conf line 2: invalid key or action",
        Gio.DBusError.FAILED)
    assert keyboard._dbus_message(error) == "default.conf line 2: invalid key or action"


# -- switched off under Advanced (#230) ---------------------------------------------

def test_switched_off_a_persons_files_are_refused_with_the_reason(kb):
    kb.set_enabled(False)
    with pytest.raises(KeymapError, match="switched off on this computer"):
        kb.set(1001, {"default.conf": CAPS}, active_uid=1001)
    assert 1001 not in kb.configs
    assert kb.fake.calls == [], "keyd is not running; nothing is asked of it"


def test_switched_off_no_files_is_fine(kb):
    kb.set_enabled(False)
    assert kb.set(1001, {}, active_uid=1001) is False


def test_switched_off_app_keys_are_refused(kb):
    kb.set_enabled(False)
    with pytest.raises(KeymapError, match="switched off on this computer"):
        kb.bind(1001, ["a = b"], active_uid=1001)


def test_switching_off_forgets_everyone_and_writes_nothing(kb):
    kb.set(1001, {"default.conf": CAPS}, active_uid=1001)
    before = len(kb.fake.calls)
    kb.set_enabled(False)
    assert kb.configs == {}
    assert kb.refresh(1001) is False
    assert len(kb.fake.calls) == before
    kb.set_enabled(True)
    kb.set(1001, {"default.conf": CAPS}, active_uid=1001)
    assert 1001 in kb.configs


def test_set_running_enables_or_disables_keyd():
    from kosherd.keyboard import set_running

    seen = []

    def run(argv, **_kw):
        seen.append(argv)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    set_running(True, run=run)
    set_running(False, wait=False, run=run)
    assert seen == [["systemctl", "enable", "--now", "keyd.service"],
                    ["systemctl", "disable", "--now", "--no-block", "keyd.service"]]

    def fails(argv, **_kw):
        return SimpleNamespace(returncode=5, stdout="", stderr="Unit keyd.service not found.")

    with pytest.raises(KeymapError, match="not found"):
        set_running(True, run=fails)
