"""The boot menu is quieted through user.cfg, which reaches installed machines.

bootupd writes its static grub.cfg at install and never again, so a
snippet in the image reaches fresh installs only — the first machine
updated to it still showed the menu. The static preamble sources
${prefix}/user.cfg after it sets its own visible one-second menu, and
kosherd already owns that file for the boot password, so the quiet lines
live there, written at every start and merged with the password.
"""

from __future__ import annotations

import errno

import pytest

from kosherd import daemon


def test_the_quiet_lines_are_a_hidden_countdown_not_no_countdown():
    assert "set timeout_style=hidden" in daemon.QUIET_MENU_LINES
    assert "set timeout=1" in daemon.QUIET_MENU_LINES
    assert "set timeout=0" not in daemon.QUIET_MENU_LINES, "zero leaves no way in"


def test_rendering_keeps_the_password_and_adds_the_quiet_lines_once():
    fresh = daemon.render_grub_user_cfg("", None)
    assert fresh == "set timeout_style=hidden\nset timeout=1\n"
    with_password = daemon.render_grub_user_cfg("GRUB2_PASSWORD=grub.pbkdf2.sha512.abc\n", None)
    assert with_password.splitlines() == ["GRUB2_PASSWORD=grub.pbkdf2.sha512.abc",
                                          "set timeout_style=hidden", "set timeout=1"]
    # A second pass over our own output changes nothing.
    assert daemon.render_grub_user_cfg(with_password, None) == with_password
    # A new password replaces the old line and keeps the quiet lines.
    replaced = daemon.render_grub_user_cfg(with_password, "grub.pbkdf2.sha512.new")
    assert replaced.splitlines()[0] == "GRUB2_PASSWORD=grub.pbkdf2.sha512.new"
    assert replaced.count("timeout_style") == 1


def test_writing_is_idempotent_and_private(tmp_path):
    path = tmp_path / "boot" / "grub2" / "user.cfg"
    assert daemon.write_grub_user_cfg(path=path) is True
    assert path.read_text() == "set timeout_style=hidden\nset timeout=1\n"
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    assert daemon.write_grub_user_cfg(path=path) is False, "nothing to do the second time"
    assert daemon.write_grub_user_cfg(password_digest="grub.pbkdf2.sha512.x", path=path) is True
    assert path.read_text().startswith("GRUB2_PASSWORD=grub.pbkdf2.sha512.x\n")
    assert daemon.write_grub_user_cfg(path=path) is False, "the password is kept"


def test_a_read_only_boot_partition_is_remounted_for_the_write(tmp_path, monkeypatch):
    path = tmp_path / "boot" / "grub2" / "user.cfg"
    path.parent.mkdir(parents=True)
    calls = []
    real_write = daemon.Path.write_text
    state = {"ro": True}

    def flaky_write(self, text, *a, **k):
        if self == path and state["ro"]:
            raise OSError(errno.EROFS, "Read-only file system")
        return real_write(self, text, *a, **k)

    def fake_run(argv, **k):
        calls.append(argv)
        if "remount,rw" in argv:
            state["ro"] = False

        class R:
            returncode = 0
            stderr = ""
        return R()

    monkeypatch.setattr(daemon.Path, "write_text", flaky_write)
    monkeypatch.setattr(daemon.subprocess, "run", fake_run)
    assert daemon.write_grub_user_cfg(path=path) is True
    assert ["mount", "-o", "remount,rw", str(tmp_path / "boot")] in calls
    assert ["mount", "-o", "remount,ro", str(tmp_path / "boot")] in calls
    assert "timeout_style=hidden" in path.read_text()


def test_the_daemon_quiets_the_menu_at_every_start():
    import inspect

    source = inspect.getsource(daemon.Daemon.run)
    assert "_quiet_boot_menu" in source
