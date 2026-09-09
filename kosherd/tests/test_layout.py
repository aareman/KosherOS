"""The desktop layouts: one setting, three desktops, applied at sign-in.

A parent picks how an account's desktop looks; kosherd keeps the choice;
kosher-layout applies it as the person signs in. The pieces live in four
places (the policy, the daemon, the sign-in helper, and the image's dconf
defaults and shell extensions), and the interesting failures are the ones
between them: an extension enabled under a uuid the image does not ship,
a default the helper cannot override, a layout the login screen does not
know which session to start.
"""

import configparser
import json
import re
import tomllib
from pathlib import Path

import pytest

from kosherd import layout as layout_mod
from kosherd.daemon import Daemon
from kosherd.policy import (
    DEFAULT_LAYOUT,
    LAYOUT_LABELS,
    LAYOUTS,
    Policy,
    PolicyError,
    UserPolicy,
)

ROOT = Path(__file__).parents[2]
FILES = ROOT / "os-image/files"
CONTAINERFILE = (ROOT / "os-image/Containerfile").read_text()
DESKTOP_DCONF = FILES / "etc/dconf/db/local.d/20-kosheros-desktop"
LOCKS = FILES / "etc/dconf/db/local.d/locks/10-kosheros-locks"
NIRI_CONFIG = FILES / "etc/niri/config.kdl"
NOCTALIA_SEED = FILES / "usr/share/kosher/desktop/noctalia.toml"
USER_UNIT = FILES / "usr/lib/systemd/user/kosher-layout.service"


# -- the setting -----------------------------------------------------------------

def test_every_layout_has_a_label_and_classic_is_the_default():
    assert set(LAYOUT_LABELS) == set(LAYOUTS)
    assert DEFAULT_LAYOUT == "classic"


def test_the_layout_round_trips_and_the_default_stays_out_of_the_file():
    tiled = UserPolicy(uid=1001, username="t", mode="filtered", layout="tiling")
    plain = UserPolicy(uid=1002, username="p", mode="filtered")
    doc = Policy(revision=1, users=[tiled, plain]).to_dict()
    assert doc["users"][0]["layout"] == "tiling"
    assert "layout" not in doc["users"][1], "existing policies stay byte-identical"
    back = Policy.from_dict(doc)
    assert back.user(1001).layout == "tiling"
    assert back.user(1002).layout == "classic"


def test_the_schema_rejects_a_layout_nobody_implements():
    doc = Policy(revision=1, users=[UserPolicy(uid=1001, username="t",
                                               mode="filtered")]).to_dict()
    doc["users"][0]["layout"] = "cosmic"
    with pytest.raises(PolicyError):
        Policy.from_dict(doc)


# -- the daemon ------------------------------------------------------------------

def _daemon(users):
    daemon = Daemon.__new__(Daemon)
    daemon.policy = Policy(revision=1, users=users)
    daemon.connection = None
    daemon.saved = 0
    daemon.applied = 0
    daemon.sessions_set = []
    daemon._save_only = lambda: setattr(daemon, "saved", daemon.saved + 1)
    daemon._save_and_apply = lambda: setattr(daemon, "applied", daemon.applied + 1)
    daemon._accounts_set_session = lambda uid, layout: daemon.sessions_set.append((uid, layout))
    return daemon


def test_setting_a_layout_saves_without_re_rendering_enforcement():
    daemon = _daemon([UserPolicy(uid=1001, username="t", mode="filtered")])
    daemon.impl_SetLayout(1001, "tiling")
    assert daemon.policy.user(1001).layout == "tiling"
    assert daemon.saved == 1
    assert daemon.applied == 0, "the firewall and proxy do not care about the shell"


def test_setting_a_layout_tells_the_login_screen_which_session():
    daemon = _daemon([UserPolicy(uid=1001, username="t", mode="filtered")])
    daemon.impl_SetLayout(1001, "advanced")
    assert daemon.sessions_set == [(1001, "advanced")]


def test_each_layout_maps_to_a_login_session():
    assert set(Daemon.LAYOUT_SESSIONS) == set(LAYOUTS)
    assert Daemon.LAYOUT_SESSIONS["advanced"] == "niri"
    assert Daemon.LAYOUT_SESSIONS["classic"] == Daemon.LAYOUT_SESSIONS["tiling"] == "gnome"


def test_an_unknown_layout_or_account_is_refused():
    daemon = _daemon([UserPolicy(uid=1001, username="t", mode="filtered")])
    with pytest.raises(PolicyError):
        daemon.impl_SetLayout(1001, "cosmic")
    with pytest.raises(PolicyError):
        daemon.impl_SetLayout(4242, "tiling")
    assert daemon.saved == 0


def test_a_failed_save_rolls_the_layout_back():
    daemon = _daemon([UserPolicy(uid=1001, username="t", mode="filtered")])

    def boom():
        raise OSError("disk full")

    daemon._save_only = boom
    with pytest.raises(OSError):
        daemon.impl_SetLayout(1001, "tiling")
    assert daemon.policy.user(1001).layout == "classic"
    assert daemon.sessions_set == []


def test_the_helper_gets_its_own_layout_and_strangers_get_the_default():
    daemon = _daemon([UserPolicy(uid=1001, username="t", mode="filtered",
                                 layout="tiling")])
    assert daemon.impl_GetMyLayout(_uid=1001).unpack() == ("tiling",)
    assert daemon.impl_GetMyLayout(_uid=1002).unpack() == ("classic",)


# -- the sign-in helper ----------------------------------------------------------

def test_every_layout_has_a_plan_and_the_advanced_one_falls_back_to_classic():
    assert set(layout_mod.PLANS) == set(LAYOUTS)
    # Someone with the advanced layout who signs in to GNOME anyway should
    # get a sane desktop, not a bare one.
    assert layout_mod.plan("advanced") == layout_mod.plan("classic")
    assert layout_mod.plan("nonsense") == layout_mod.plan(DEFAULT_LAYOUT)


def _enabled(layout: str) -> list[str]:
    for schema, key, value in layout_mod.plan(layout):
        if (schema, key) == ("org.gnome.shell", "enabled-extensions"):
            return re.findall(r"'([^']+)'", value)
    raise AssertionError(f"{layout} does not set the extension list")


def test_classic_is_a_taskbar_and_tiling_is_paperwm_never_both():
    assert layout_mod.EXT_DASH_TO_PANEL in _enabled("classic")
    assert layout_mod.EXT_PAPERWM not in _enabled("classic")
    assert layout_mod.EXT_PAPERWM in _enabled("tiling")
    assert layout_mod.EXT_DASH_TO_PANEL not in _enabled("tiling"), \
        "Dash to Panel and PaperWM fight over the screen"
    # Tray icons for everyone: Bluetooth, cloud clients and the like.
    for layout in ("classic", "tiling"):
        assert layout_mod.EXT_APPINDICATOR in _enabled(layout)


def test_a_layout_overrides_everything_the_other_wrote():
    # The plans are complete, not diffs: switching an account from tiling
    # back to classic must undo every key tiling set.
    keys = {layout: {(s, k) for s, k, _v in layout_mod.plan(layout)}
            for layout in ("classic", "tiling")}
    assert keys["classic"] == keys["tiling"]


def test_classic_has_one_workspace_and_minimise_buttons():
    plan = {(s, k): v for s, k, v in layout_mod.plan("classic")}
    assert plan[("org.gnome.mutter", "dynamic-workspaces")] == "false"
    assert plan[("org.gnome.desktop.wm.preferences", "num-workspaces")] == "1"
    assert "minimize" in plan[("org.gnome.desktop.wm.preferences", "button-layout")]
    assert plan[("org.gnome.desktop.interface", "enable-hot-corners")] == "false"


def test_applying_goes_on_past_a_failure_and_reports_it():
    calls = []

    class Result:
        def __init__(self, rc, err=""):
            self.returncode, self.stderr = rc, err

    def run(cmd, **_kw):
        calls.append(cmd)
        return Result(1, "No such schema") if "org.gnome.mutter" in cmd else Result(0)

    failures = layout_mod.apply_gsettings(layout_mod.plan("classic"), run=run)
    assert len(calls) == len(layout_mod.plan("classic"))
    assert all(c[:2] == ["gsettings", "set"] for c in calls)
    assert failures and all("org.gnome.mutter" in f for f in failures)


def test_noctalia_is_seeded_once_and_never_overwritten(tmp_path):
    seed = tmp_path / "seed.toml"
    seed.write_text('[shell]\npolkit_agent = true\n')
    home = tmp_path / "home"
    home.mkdir()
    assert layout_mod.seed_noctalia(home, seed) is True
    target = home / layout_mod.NOCTALIA_CONFIG
    assert target.read_text() == seed.read_text()
    target.write_text("# mine now\n")
    assert layout_mod.seed_noctalia(home, seed) is False
    assert target.read_text() == "# mine now\n"


def test_no_seed_means_nothing_to_do(tmp_path):
    assert layout_mod.seed_noctalia(tmp_path, tmp_path / "missing.toml") is False


def test_under_niri_is_read_from_the_session():
    assert layout_mod.under_niri("niri")
    assert layout_mod.under_niri("niri:wlroots")
    assert not layout_mod.under_niri("gnome")
    assert not layout_mod.under_niri("")


def test_dry_run_prints_the_plan_and_touches_nothing(capsys, monkeypatch):
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "GNOME")
    assert layout_mod.main(["--layout", "tiling", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert layout_mod.EXT_PAPERWM in out
    assert out.count("gsettings set") == len(layout_mod.plan("tiling"))


# -- the image -------------------------------------------------------------------

def _dconf(path: Path) -> configparser.ConfigParser:
    cp = configparser.ConfigParser()
    cp.read_string(path.read_text())
    return cp


def test_the_machine_wide_default_is_the_classic_layout():
    cp = _dconf(DESKTOP_DCONF)
    enabled = re.findall(r"'([^']+)'", cp["org/gnome/shell"]["enabled-extensions"])
    assert enabled == _enabled("classic"), \
        "the image default and the helper's classic plan must be the same desktop"
    assert cp["org/gnome/shell/extensions/dash-to-panel"]["hide-overview-on-startup"] == "true"
    assert cp["org/gnome/mutter"]["dynamic-workspaces"] == "false"


def test_the_start_button_is_the_kosheros_mark():
    cp = _dconf(DESKTOP_DCONF)
    icon = cp["org/gnome/shell/extensions/dash-to-panel"]["show-apps-icon-file"].strip("'")
    # Shipped by the branding COPY in the Containerfile.
    assert icon.startswith("/usr/share/kosher/branding/")
    assert (ROOT / "branding" / Path(icon).name).exists()


def test_the_keys_the_helper_writes_are_not_locked():
    # A dconf lock is machine-wide and would silently defeat the per-account
    # write at sign-in; the tiling layout would then be impossible.
    locked = {line.strip() for line in LOCKS.read_text().splitlines()
              if line.strip() and not line.startswith("#")}
    for layout in LAYOUTS:
        for schema, key, _v in layout_mod.plan(layout):
            path = "/" + schema.replace(".", "/") + "/" + key
            assert path not in locked, f"{path} is locked; the helper cannot set it"


def test_extension_installation_is_locked_off():
    cp = _dconf(DESKTOP_DCONF)
    assert cp["org/gnome/shell"]["allow-extension-installation"] == "false"
    assert "/org/gnome/shell/allow-extension-installation" in LOCKS.read_text()


def test_the_image_ships_every_extension_the_helper_enables():
    # Enabling a uuid the image lacks is silently ignored by GNOME Shell —
    # the account would get a plain desktop with no error anywhere.
    assert "gnome-shell-extension-dash-to-panel" in CONTAINERFILE
    assert "gnome-shell-extension-appindicator" in CONTAINERFILE
    assert f"/usr/share/gnome-shell/extensions/{layout_mod.EXT_PAPERWM}" in CONTAINERFILE
    assert "glib-compile-schemas" in CONTAINERFILE


def test_the_image_ships_the_advanced_session_and_its_shell():
    assert re.search(r"\bniri\b", CONTAINERFILE)
    assert re.search(r"\bnoctalia\b", CONTAINERFILE)
    # Flatpak apps need a portal backend outside GNOME.
    assert "xdg-desktop-portal-gtk" in CONTAINERFILE
    # A second GNOME session in the login screen's list is one choice too
    # many for the people this is for.
    assert "gnome-classic-session" in CONTAINERFILE


def test_the_helper_runs_at_every_graphical_sign_in():
    unit = USER_UNIT.read_text()
    assert "WantedBy=graphical-session.target" in unit
    assert "ExecStart=/usr/bin/kosher-layout" in unit
    assert "systemctl --global enable kosher-layout.service" in CONTAINERFILE
    pyproject = (ROOT / "kosherd/pyproject.toml").read_text()
    assert 'kosher-layout = "kosherd.layout:main"' in pyproject


def test_the_niri_session_starts_the_shell_and_reads_the_system_keyboard():
    text = NIRI_CONFIG.read_text()
    assert re.search(r'^spawn-at-startup "noctalia"', text, re.M)
    assert "waybar" not in text
    # An empty xkb block means niri takes the layout from localectl, which
    # is where the wizard and GNOME Settings put it — so Hebrew is there
    # without anyone editing a file.
    xkb = re.search(r"xkb \{(.*?)\}", text, re.S).group(1)
    assert not re.search(r"^\s*layout ", xkb, re.M)


def test_the_niri_binds_are_unique():
    # niri rejects a configuration with the same key bound twice — and then
    # falls back to its built-in defaults, which start waybar and alacritty,
    # neither of which the image has. The whole desktop would be a black
    # screen with a hotkey card.
    text = NIRI_CONFIG.read_text()
    binds = re.search(r"^binds \{(.*)^\}", text, re.S | re.M).group(1)
    keys = re.findall(r"^\s*([A-Za-z0-9_+]+)\s[^{]*\{", binds, re.M)
    dupes = {k for k in keys if keys.count(k) > 1}
    assert not dupes, dupes


def test_the_niri_lock_screen_and_launcher_are_the_shells():
    text = NIRI_CONFIG.read_text()
    assert "noctalia msg session lock" in text
    assert "noctalia msg panel-toggle launcher" in text
    assert "swaylock" not in text and "fuzzel" not in text and "alacritty" not in text


def test_the_noctalia_seed_parses_and_turns_the_polkit_agent_on():
    doc = tomllib.loads(NOCTALIA_SEED.read_text())
    # Without an agent the admin app's password prompt has nowhere to
    # appear in this session, and every privileged action just fails.
    assert doc["shell"]["polkit_agent"] is True
    assert doc["shell"]["telemetry_enabled"] is False
    assert doc["wallpaper"]["directory"] == "/usr/share/backgrounds/kosheros"
    assert layout_mod.NOCTALIA_SEED == Path("/usr/share/kosher/desktop/noctalia.toml")
    assert NOCTALIA_SEED.relative_to(FILES) == Path(str(layout_mod.NOCTALIA_SEED).lstrip("/"))


def test_the_wallpaper_directory_gets_a_raster_copy():
    # Noctalia is pointed at the directory; a PNG is what every shell draws.
    assert "/usr/share/backgrounds/kosheros/kosheros.png" in CONTAINERFILE


def test_the_dconf_keyfile_parses():
    _dconf(DESKTOP_DCONF)


def test_the_polkit_action_for_reading_your_own_layout_needs_no_prompt():
    policy = (FILES / "usr/share/polkit-1/actions/org.kosherlinux.policy").read_text()
    block = re.search(r'<action id="org.kosherlinux.read-own-settings">(.*?)</action>',
                      policy, re.S).group(1)
    assert "<allow_active>yes</allow_active>" in block
    rules = (FILES / "etc/polkit-1/rules.d/49-kosher-admin.rules").read_text()
    # The admin rule beats the action default, so admins must be listed too
    # or they alone would get a password prompt at sign-in.
    assert "org.kosherlinux.read-own-settings" in rules


def test_the_client_speaks_both_methods():
    src = (ROOT / "kosherd/src/kosherd/client.py").read_text()
    assert '"SetLayout", "(is)"' in src
    assert '"GetMyLayout"' in src
    xml = (ROOT / "kosherd/src/kosherd/daemon.py").read_text()
    assert '<method name="SetLayout">' in xml
    assert '<method name="GetMyLayout">' in xml


def test_the_schema_copies_agree_on_the_layout():
    for path in (ROOT / "policy/schema/policy.schema.json",
                 ROOT / "kosherd/src/kosherd/data/policy.schema.json"):
        schema = json.loads(path.read_text())
        assert schema["$defs"]["user"]["properties"]["layout"]["enum"] == list(LAYOUTS)
