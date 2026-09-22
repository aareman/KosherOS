"""A private X display for code that builds widgets without anyone watching.

The apps' widget tests construct real GTK windows and present dialogs into
them. Where that drawing lands is decided by the environment, and on a
developer's desktop the environment says "here": GTK4 tries Wayland before
X11, so even under ``xvfb-run`` — which only replaces ``DISPLAY`` — every
window opened on the real desktop the moment ``WAYLAND_DISPLAY`` was set.
An agent running the suites made the screen flicker with test windows.

:func:`ensure` makes the decision here instead. It starts an Xvfb server of
its own, points GTK at it and at nothing else, and stops the server when
the process exits. Without Xvfb it leaves GTK with no display at all, so
the widget tests skip rather than borrow the desktop. Nothing in the
daemon calls this; it is for the test suites and for anyone who wants to
drive an app headlessly.
"""

from __future__ import annotations

import atexit
import os
import shutil
import subprocess

# Set for the process (and everything it starts) once a display is ours,
# so a nested pytest or a subprocess does not start a second server.
MARKER = "KOSHER_VIRTUAL_DISPLAY"

_server: subprocess.Popen | None = None


def ensure(env: os._Environ | dict = os.environ) -> str | None:
    """Point GTK at a virtual display, starting one when needed.

    Returns a short description of the display for a test report header,
    or ``None`` when no Xvfb is available — in which case the environment
    has been arranged so that GTK finds no display at all.
    """
    # Never the desktop: not over Wayland, and not over the session's X
    # server either. GTK4 reads these at init, before any window exists.
    env.pop("WAYLAND_DISPLAY", None)
    env["GDK_BACKEND"] = "x11"
    # Nor the desktop's accessibility bus or settings: the AT-SPI backend
    # would connect to (or launch) the person's own, and dconf would make
    # the tests depend on their theme.
    env["GTK_A11Y"] = "none"
    env["NO_AT_BRIDGE"] = "1"
    env["GSETTINGS_BACKEND"] = "memory"

    if env.get(MARKER):
        return f"{env.get('DISPLAY', '?')} (virtual, inherited)"

    xvfb = shutil.which("Xvfb")
    if xvfb is None:
        env["DISPLAY"] = ""
        return None

    display = _start(xvfb)
    env["DISPLAY"] = display
    env[MARKER] = display
    return f"{display} (virtual, Xvfb)"


def _start(xvfb: str) -> str:
    """Start Xvfb on a free display number and return it as ``:N``."""
    global _server
    read_end, write_end = os.pipe()
    try:
        _server = subprocess.Popen(
            [xvfb, "-displayfd", str(write_end), "-screen", "0", "1600x1000x24",
             "-nolisten", "tcp"],
            pass_fds=(write_end,), stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    finally:
        os.close(write_end)
    atexit.register(stop)
    # Xvfb writes the display number it chose, then a newline, once it is
    # listening. If it dies first the pipe closes and the read is empty.
    with os.fdopen(read_end) as pipe:
        number = pipe.readline().strip()
    if not number:
        code = _server.wait()
        raise RuntimeError(f"Xvfb exited with status {code} before choosing a display")
    return f":{number}"


def stop() -> None:
    """Stop the server this process started, if any."""
    global _server
    if _server is None:
        return
    _server.terminate()
    try:
        _server.wait(timeout=5)
    except subprocess.TimeoutExpired:
        _server.kill()
        _server.wait()
    _server = None
