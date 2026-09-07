"""The proxy unit must load the filter script from /run, never /usr.

mitmproxy loads a --scripts file only when its mtime exceeds an initial
0.0, and every file under /usr on an ostree image has mtime 0 — so a
script pointed at /usr is silently never loaded and the proxy runs with
no filtering at all. This cost two sessions of heisenbug: any copy of
the script gained a real mtime and appeared to fix the filter.
"""

from pathlib import Path

UNIT = (Path(__file__).parents[2]
        / "os-image/files/usr/lib/systemd/system/kosher-mitm.service").read_text()


def test_the_script_is_loaded_from_run_not_usr():
    exec_start = [l for l in UNIT.splitlines() if "--scripts" in l and "ExecStart" not in l or "--scripts" in l]
    joined = " ".join(exec_start)
    assert "/run/kosher-mitm/kosher_filter.py" in joined
    assert "--scripts /usr/share" not in UNIT.replace("\\\n", " "), \
        "loading from /usr means mtime 0 and a silently unloaded filter"


def test_the_script_is_copied_into_run_before_start():
    assert "ExecStartPre=" in UNIT
    assert "install -m 0644" in UNIT.replace("\\\n    ", " ")
    assert "RuntimeDirectory=kosher-mitm" in UNIT
