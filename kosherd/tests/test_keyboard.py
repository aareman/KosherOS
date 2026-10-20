"""Per-person key remapping through keyd (kosherd/keyboard.py).

The parts that matter for safety are pure and tested here: what a
person's file may contain before keyd sees it, how keyd's complaints map
back to their lines, and whose config is in force as sessions come and
go. keyd itself is faked by a recorder; set KOSHER_TEST_KEYD to a keyd
binary to also run its own parser over the prepared files.
"""

import os
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from kosherd import access, keyboard
from kosherd.daemon import INTROSPECTION_XML, Daemon
from kosherd.keyboard import HEADER, HEADER_LINES, Keyboards, KeymapError, prepare

ROOT = Path(__file__).parents[2]
FILES = ROOT / "os-image/files"
CONTAINERFILE = (ROOT / "os-image/Containerfile").read_text()
BASE = FILES / "etc/keyd/default.conf"
USER_UNIT = FILES / "usr/lib/systemd/user/kosher-keyboard.service"
KEYD_DROPIN = FILES / "usr/lib/systemd/system/keyd.service.d/10-kosheros.conf"

CAPS = "[main]\ncapslock = overload(control, esc)\n"


@pytest.fixture
def data_dir(tmp_path):
    d = tmp_path / "share-keyd"
    (d / "layouts").mkdir(parents=True)
    (d / "layouts/colemak").write_text("a = a\n")
    (tmp_path / "secret").write_text("root's business\n")
    return d


# -- what a person's file may say ---------------------------------------------

def test_a_plain_file_gets_every_keyboard_and_keeps_its_lines(data_dir):
    out = prepare(CAPS, data_dir=data_dir)
    assert out == HEADER + CAPS
    assert out.splitlines()[HEADER_LINES:] == CAPS.splitlines()


def test_their_own_ids_are_blanked_line_for_line(data_dir):
    text = "[ids]\nm:046d:b01d\n-*\n\n[main]\na = b\n"
    out = prepare(text, data_dir=data_dir).splitlines()
    assert out[:HEADER_LINES] == ["[ids]", "*"]
    mine = out[HEADER_LINES:]
    assert len(mine) == len(text.splitlines()), "line numbers still match the file"
    assert mine[:3] == ["", "", ""] and mine[4:] == ["[main]", "a = b"]
    # keyd reads a section header after trimming spaces, so does this.
    assert "046d" not in prepare("  [ids]  \n046d:b01d\n[main]\n", data_dir=data_dir)


@pytest.mark.parametrize("line", [
    "a = command(touch /etc/owned)",
    "capslock = overload(control, command(id))",
    "a = macro(command(id))",
    "a = command (id)",
    "a = COMMAND(id)",
    "[nav]\nh = command(id)",
])
def test_command_is_refused_because_keyd_runs_it_as_root(data_dir, line):
    with pytest.raises(KeymapError, match="command"):
        prepare("[main]\n" + line + "\n", data_dir=data_dir)


def test_a_comment_may_mention_command(data_dir):
    text = "# no command(...) here, keyd would run it as root\n[main]\na = b\n"
    assert prepare(text, data_dir=data_dir) == HEADER + text


def test_the_error_names_the_line(data_dir):
    with pytest.raises(KeymapError, match=r"^line 3:"):
        prepare("[main]\na = b\nc = command(id)\n", data_dir=data_dir)


def test_includes_reach_only_what_keyd_ships(data_dir):
    ok = "[main]\ninclude layouts/colemak\n"
    assert prepare(ok, data_dir=data_dir) == HEADER + ok
    for bad in ("../secret", "/etc/shadow", "layouts/../../secret", "layouts/missing",
                "layouts", "check/candidate.conf", "default.conf"):
        with pytest.raises(KeymapError, match="include"):
            prepare(f"[main]\ninclude {bad}\n", data_dir=data_dir)


def test_an_include_cannot_escape_through_a_link(data_dir):
    (data_dir / "layouts/sneaky").symlink_to(data_dir.parent / "secret")
    with pytest.raises(KeymapError, match="include"):
        prepare("[main]\ninclude layouts/sneaky\n", data_dir=data_dir)


def test_too_many_includes(data_dir):
    text = "[main]\n" + "include layouts/colemak\n" * (keyboard.MAX_INCLUDES + 1)
    with pytest.raises(KeymapError, match="includes"):
        prepare(text, data_dir=data_dir)


@pytest.mark.parametrize("text,match", [
    ("#" * (keyboard.MAX_BYTES + 1), "larger"),
    ("\n" * (keyboard.MAX_LINES + 1), "lines"),
    ("[main]\na = " + "b" * keyboard.MAX_LINE + "\n", "longer"),
    ("".join(f"[l{i}]\n" for i in range(keyboard.MAX_SECTIONS + 1)), "sections"),
    ("[main]\na = b\x00\n", "control"),
    ("[main]\na = \x1b[31m\n", "control"),
])
def test_what_would_trip_keyds_own_limits_is_refused_first(data_dir, text, match):
    # keyd checks its limits with assert(), so a file past them would
    # abort the daemon instead of being refused.
    with pytest.raises(KeymapError, match=match):
        prepare(text, data_dir=data_dir)


def test_crlf_and_tabs_and_unicode_are_fine(data_dir):
    out = prepare("[main]\r\na\t= ä\r\n", data_dir=data_dir)
    assert out.splitlines()[HEADER_LINES:] == ["[main]", "a\t= ä"]


def test_the_computers_own_config_remaps_nothing(data_dir):
    text = BASE.read_text()
    lines = [ln.strip() for ln in text.splitlines()
             if ln.strip() and not ln.strip().startswith("#")]
    assert lines == ["[ids]", "*", "[main]"]


# -- keyd's complaints, in the person's line numbers --------------------------

def test_keyd_check_output_maps_back_to_their_lines():
    out = ("Parsing /run/keyd/check/candidate.conf\n"
           "\tWARNING: /run/keyd/check/candidate.conf:9: failed to resolve include path x\n"
           "\tWARNING: /run/keyd/check/candidate.conf:8: invalid key or action\n"
           "\tWARNING: /usr/share/keyd/layouts/de:3: invalid key or action\n")
    assert keyboard.explain_check(out, "/run/keyd/check/candidate.conf") == [
        "line 7: failed to resolve include path x",
        "line 6: invalid key or action",
        "/usr/share/keyd/layouts/de line 3: invalid key or action",
    ]


# -- whose config is in force ---------------------------------------------------

class FakeKeyd:
    """Records what kosherd asks keyd for; `check_output` makes check fail."""

    def __init__(self):
        self.calls = []
        self.check_output = None
        self.checked_as = None

    def __call__(self, argv, **kwargs):
        self.calls.append(argv[1])
        if argv[1] == "check":
            self.checked_as = {k: kwargs.get(k) for k in ("user", "group", "extra_groups")}
            assert Path(argv[2]).read_text().startswith(HEADER)
            if self.check_output:
                return SimpleNamespace(returncode=255, stdout=self.check_output.replace(
                    "PATH", argv[2]), stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    @property
    def reloads(self):
        return self.calls.count("reload")


@pytest.fixture
def kb(tmp_path, data_dir):
    run = FakeKeyd()
    k = Keyboards(system_dir=tmp_path / "run-keyd", base=BASE, data_dir=data_dir, run=run)
    k.fake = run
    return k


def live(kb):
    return kb.live.read_text()


def test_a_person_on_the_screen_gets_theirs(kb):
    assert kb.set(1001, CAPS, active_uid=1001)
    assert live(kb) == HEADER + CAPS
    assert kb.fake.calls == ["check", "reload"]
    assert not (kb.system_dir / "check/candidate.conf").exists(), "the candidate is cleaned up"


def test_keyd_check_runs_as_nobody(kb):
    kb.set(1001, CAPS, active_uid=1001)
    assert kb.fake.checked_as == {"user": "nobody", "group": "nobody", "extra_groups": []}


def test_switching_people_switches_configs_and_the_login_screen_gets_none(kb):
    kb.set(1001, CAPS, active_uid=1001)
    other = "[main]\na = b\n"
    kb.set(1002, other, active_uid=1001)
    assert live(kb) == HEADER + CAPS, "1002 is signed in but not on the screen"
    kb.refresh(1002)
    assert live(kb) == HEADER + other
    kb.refresh(None)
    assert live(kb) == BASE.read_text(), "the login screen"
    kb.refresh(1003)
    assert live(kb) == BASE.read_text(), "someone with no file"


def test_nothing_reloads_when_nothing_changed(kb):
    kb.set(1001, CAPS, active_uid=1001)
    reloads = kb.fake.reloads
    assert not kb.refresh(1001)
    assert kb.fake.reloads == reloads


def test_a_keyd_restart_is_noticed(kb):
    kb.set(1001, CAPS, active_uid=1001)
    # keyd.service put the computer's config back as it restarted.
    kb.live.write_text(BASE.read_text())
    assert kb.refresh(1001)
    assert live(kb) == HEADER + CAPS


def test_a_refused_file_leaves_what_was_in_force(kb):
    kb.set(1001, CAPS, active_uid=1001)
    kb.fake.check_output = "\tWARNING: PATH:5: invalid key or action\n"
    with pytest.raises(KeymapError, match="line 3: invalid key or action"):
        kb.set(1001, "[main]\na = b\nfoo = bar\n", active_uid=1001)
    assert live(kb) == HEADER + CAPS
    with pytest.raises(KeymapError, match="command"):
        kb.set(1001, "[main]\na = command(id)\n", active_uid=1001)
    assert live(kb) == HEADER + CAPS
    assert kb.configs[1001] == HEADER + CAPS


def test_an_empty_file_is_no_remaps(kb):
    kb.set(1001, CAPS, active_uid=1001)
    kb.set(1001, "  \n", active_uid=1001)
    assert 1001 not in kb.configs and live(kb) == BASE.read_text()


def test_signing_out_forgets_the_config(kb):
    kb.set(1001, CAPS, active_uid=1001)
    kb.set(1002, "[main]\na = b\n", active_uid=1001)
    kb.forget_all_but({1002})
    assert set(kb.configs) == {1002}


def test_a_keyd_that_will_not_reload_is_not_fatal(kb):
    kb.run = lambda argv, **kw: SimpleNamespace(returncode=1, stdout="", stderr="no socket")
    assert kb.refresh(None) is False, "logged, not raised"
    assert live(kb) == BASE.read_text()


# -- the daemon method ----------------------------------------------------------

def test_the_method_is_yours_alone_and_needs_no_admin():
    assert access.ACTIONS["SetMyKeyboard"] == access.ACTION_SET_OWN
    assert "SetMyKeyboard" in access.UID_AWARE, "the uid comes from the bus"
    assert "SetMyKeyboard" not in access.GUARDIAN_GATED
    assert '<method name="SetMyKeyboard">' in INTROSPECTION_XML


def test_the_daemon_applies_for_the_caller_while_they_are_on_the_screen(kb):
    daemon = Daemon.__new__(Daemon)
    daemon.connection = None
    daemon.keyboards = kb
    daemon._active_seat_uid = lambda: 1001
    daemon.impl_SetMyKeyboard(CAPS, _uid=1001)
    assert live(kb) == HEADER + CAPS
    daemon.impl_SetMyKeyboard("[main]\nb = a\n", _uid=1002)
    assert live(kb) == HEADER + CAPS, "1002 is not on the screen"
    with pytest.raises(ValueError):
        daemon.impl_SetMyKeyboard("[main]\na = command(id)\n", _uid=1001)


# -- the image ------------------------------------------------------------------

def test_keyd_is_built_from_a_pinned_checked_release_reading_run():
    assert "as keyd-build" in CONTAINERFILE
    assert "ARG KEYD_VERSION=2.6.0" in CONTAINERFILE
    assert "sha256sum -c -" in CONTAINERFILE.split("as keyd-build")[1].split("FROM ")[0]
    assert "make PREFIX=/usr CONFIG_DIR=/run/keyd" in CONTAINERFILE
    assert "make install PREFIX=/usr CONFIG_DIR=/run/keyd" in CONTAINERFILE, \
        "both, or the man page and the binary disagree"
    assert "COPY --from=keyd-build /out/usr/ /usr/" in CONTAINERFILE
    assert "systemctl enable keyd.service" in CONTAINERFILE


def test_nobody_gets_raw_input_or_keyds_socket():
    # The whole design rests on this: an account that can read /dev/input
    # can read the guardian password, and one on keyd's socket is root.
    assert "keyd-application-mapper" in CONTAINERFILE and \
        "! test -e /usr/bin/keyd-application-mapper" in CONTAINERFILE
    assert 'test -z "$(getent group keyd | cut -d: -f4)"' in CONTAINERFILE
    src = ROOT / "kosherd/src/kosherd"
    for path in [ROOT / "os-image/Containerfile", *src.glob("*.py"),
                 *FILES.rglob("*")]:
        if not path.is_file() or path.suffix in (".png", ".svg", ".jpg", ".onnx"):
            continue
        try:
            text = path.read_text()
        except UnicodeDecodeError:
            continue
        for group in ("keyd", "input", "uinput"):
            assert f"-aG {group} " not in text and f"-G {group} " not in text, path


def test_keyd_starts_from_the_computers_config_and_comes_back():
    text = KEYD_DROPIN.read_text()
    assert "ExecStartPre=/usr/bin/install -D -m 0644 /etc/keyd/default.conf " \
           "/run/keyd/default.conf" in text
    assert "Restart=always" in text


def test_every_session_runs_the_helper():
    unit = USER_UNIT.read_text()
    assert "ExecStart=/usr/bin/kosher-keyboard --watch" in unit
    assert "WantedBy=graphical-session.target" in unit
    assert "ConditionUser=!@system" in unit
    assert "systemctl --global enable kosher-keyboard.service" in CONTAINERFILE
    assert 'kosher-keyboard = "kosherd.keyboard:main"' in \
        (ROOT / "kosherd/pyproject.toml").read_text()


def test_keyd_is_credited():
    third = (ROOT / "THIRD-PARTY.md").read_text()
    assert "keyd" in third and "MIT" in third.split("keyd", 1)[1][:400]


# -- keyd's own parser, when one is at hand -------------------------------------

KEYD = os.environ.get("KOSHER_TEST_KEYD") or shutil.which("keyd")


@pytest.mark.skipif(not KEYD, reason="no keyd binary (set KOSHER_TEST_KEYD)")
def test_keyd_accepts_what_prepare_makes(tmp_path, data_dir):
    path = tmp_path / "candidate.conf"
    path.write_text(prepare("[main]\ncapslock = overload(control, esc)\n\n"
                            "[global]\noverload_tap_timeout = 200\n\n"
                            "[nav]\nh = left\n", data_dir=data_dir))
    r = subprocess.run([KEYD, "check", str(path)], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    path.write_text(prepare("[main]\na = b\nfoo = bar\n", data_dir=data_dir))
    r = subprocess.run([KEYD, "check", str(path)], capture_output=True, text=True)
    assert r.returncode != 0
    assert keyboard.explain_check(r.stdout + r.stderr, str(path)) == \
        ["line 3: invalid key or action"]
