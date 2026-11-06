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
    COMPOSITORS,
    DEFAULT_COMPOSITOR,
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
HYPRLAND_CONFIG = FILES / "etc/xdg/hypr/hyprland.lua"
UMBRIEL_CONFIG = FILES / "etc/xdg/umbriel/config.toml"
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


def test_a_person_chooses_their_own_layout_and_only_their_own(monkeypatch):
    daemon = _daemon([UserPolicy(uid=1001, username="t", mode="filtered"),
                      UserPolicy(uid=1002, username="u", mode="filtered")])
    daemon.impl_SetMyLayout("tiling", _uid=1001)
    assert daemon.policy.user(1001).layout == "tiling"
    assert daemon.policy.user(1002).layout == "classic"
    assert daemon.sessions_set == [(1001, "tiling")]
    assert daemon.applied == 0
    with pytest.raises(PolicyError):
        daemon.impl_SetMyLayout("cosmic", _uid=1001)


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


def test_the_compositor_round_trips_and_the_default_stays_out_of_the_file():
    hypr = UserPolicy(uid=1001, username="t", mode="filtered", compositor="hyprland")
    plain = UserPolicy(uid=1002, username="p", mode="filtered")
    doc = Policy(revision=1, users=[hypr, plain]).to_dict()
    assert doc["users"][0]["compositor"] == "hyprland"
    assert "compositor" not in doc["users"][1]
    back = Policy.from_dict(doc)
    assert back.user(1001).compositor == "hyprland"
    assert back.user(1002).compositor == DEFAULT_COMPOSITOR == "niri"
    doc["users"][0]["compositor"] = "sway"
    with pytest.raises(PolicyError):
        Policy.from_dict(doc)


def _daemon_with_sessions(users):
    """A daemon whose login-screen call records the session it would set."""
    daemon = _daemon(users)
    daemon.sessions_named = []
    daemon._accounts_set_session = lambda uid, layout: daemon.sessions_named.append(
        (uid, daemon._session_for(uid, layout)))
    return daemon


def test_the_login_screen_is_pointed_at_the_chosen_compositor():
    daemon = _daemon_with_sessions([UserPolicy(uid=1001, username="t", mode="filtered",
                                               compositor="umbriel")])
    daemon.impl_SetMyLayout("advanced", _uid=1001)
    daemon.impl_SetMyLayout("tiling", _uid=1001)
    assert daemon.sessions_named == [(1001, "umbriel"), (1001, "gnome")]


def test_choosing_a_compositor_under_advanced_changes_the_session_at_once():
    daemon = _daemon_with_sessions([UserPolicy(uid=1001, username="t", mode="filtered",
                                               layout="advanced")])
    daemon.impl_SetMyCompositor("hyprland", _uid=1001)
    assert daemon.policy.user(1001).compositor == "hyprland"
    assert daemon.sessions_named == [(1001, "hyprland")]
    assert daemon.saved == 1 and daemon.applied == 0
    assert daemon.impl_GetMyCompositor(_uid=1001).unpack() == ("hyprland",)
    assert daemon.impl_GetMyCompositor(_uid=1002).unpack() == ("niri",)


def test_a_compositor_chosen_under_gnome_waits_for_advanced():
    daemon = _daemon_with_sessions([UserPolicy(uid=1001, username="t", mode="filtered")])
    daemon.impl_SetMyCompositor("umbriel", _uid=1001)
    assert daemon.sessions_named == [], "classic stays on GNOME"
    with pytest.raises(PolicyError):
        daemon.impl_SetMyCompositor("sway", _uid=1001)
    with pytest.raises(PolicyError):
        daemon.impl_SetMyCompositor("niri", _uid=4242)


def test_a_failed_save_rolls_the_compositor_back():
    daemon = _daemon_with_sessions([UserPolicy(uid=1001, username="t", mode="filtered",
                                               layout="advanced")])

    def boom():
        raise OSError("disk full")

    daemon._save_only = boom
    with pytest.raises(OSError):
        daemon.impl_SetMyCompositor("hyprland", _uid=1001)
    assert daemon.policy.user(1001).compositor == "niri"
    assert daemon.sessions_named == []


def test_every_compositor_has_a_login_session():
    assert set(Daemon.COMPOSITOR_SESSIONS) == set(COMPOSITORS)
    assert Daemon.COMPOSITOR_SESSIONS[DEFAULT_COMPOSITOR] == Daemon.LAYOUT_SESSIONS["advanced"]
    assert set(COMPOSITORS) == set(layout_mod.NOCTALIA_COMPOSITORS)


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
    assert layout_mod.EXT_ARCMENU in _enabled("classic"), "the Apps button"
    assert layout_mod.EXT_PAPERWM not in _enabled("classic")
    assert layout_mod.EXT_PAPERWM in _enabled("tiling")
    assert layout_mod.EXT_DASH_TO_PANEL not in _enabled("tiling"), \
        "Dash to Panel and PaperWM fight over the screen"
    assert layout_mod.EXT_ARCMENU not in _enabled("tiling")
    # Tray icons for everyone: Bluetooth, cloud clients and the like.
    for layout in ("classic", "tiling"):
        assert layout_mod.EXT_APPINDICATOR in _enabled(layout)


def test_a_layout_overrides_everything_the_other_wrote():
    # The plans are complete, not diffs: switching an account from tiling
    # back to classic must undo every key tiling set.
    keys = {layout: {(s, k) for s, k, _v in layout_mod.plan(layout)}
            for layout in ("classic", "tiling")}
    assert keys["classic"] == keys["tiling"]


def test_classic_keeps_dynamic_workspaces_and_has_minimise_buttons():
    plan = {(s, k): v for s, k, v in layout_mod.plan("classic")}
    # Written, not merely left alone: the plan is rewritten at every
    # sign-in, and accounts that got the old fixed single workspace are
    # put right by it (#132).
    assert plan[("org.gnome.mutter", "dynamic-workspaces")] == "true"
    assert plan[("org.gnome.desktop.wm.preferences", "num-workspaces")] == "4"
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


def test_the_running_compositor_is_read_from_the_session():
    assert layout_mod.running_compositor("niri") == "niri"
    assert layout_mod.running_compositor("niri:wlroots") == "niri"
    assert layout_mod.running_compositor("Hyprland") == "hyprland"
    assert layout_mod.running_compositor("Umbriel") == "umbriel"
    assert layout_mod.running_compositor("GNOME") is None
    assert layout_mod.running_compositor("") is None


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
    # GNOME's dynamic workspaces are the default and no fixed count is set (#132).
    assert "dynamic-workspaces" not in cp["org/gnome/mutter"]
    assert "num-workspaces" not in cp["org/gnome/desktop/wm/preferences"]


def test_the_start_button_is_the_bais_hamikdosh_in_the_brand_blue_and_says_apps():
    cp = _dconf(DESKTOP_DCONF)
    arc = cp["org/gnome/shell/extensions/arcmenu"]
    # A picture alone did not tell anyone where the apps were.
    assert arc["menu-button-appearance"] == "'Icon_Text'"
    assert arc["menu-button-text"] == "'Apps'"
    # A filled button, so it stands out from the taskbar's other icons.
    assert arc["menu-button-bg-color"].startswith("(true,")
    assert arc["menu-layout"] == "'windows'"
    # The Bais Hamikdosh, not the KosherOS mark: the mark is the admin app's
    # icon and read as "admin" on the taskbar. Drawn at build by branding/rasters.py.
    icon = arc["menu-button-icon"].strip("'")
    assert icon.endswith("kosheros-home.png")
    assert icon.lstrip("/") in (ROOT / "branding/rasters.py").read_text()
    # In the brand blue, on a pill it can be seen against.
    assert "rgb(29,78,216)" in arc["menu-button-fg-color"]
    assert "rgba(59,110,200" not in arc["menu-button-bg-color"]
    # And the fallback, should ArcMenu be missing from a build.
    fallback = cp["org/gnome/shell/extensions/dash-to-panel"]["show-apps-icon-file"].strip("'")
    assert fallback == arc["menu-button-icon"].strip("'")
    assert (ROOT / "branding/logo.png").exists()


def test_the_image_builds_arcmenu_from_a_pinned_tag():
    assert "ARG ARCMENU_REF=v" in CONTAINERFILE
    assert "gitlab.com/arcmenu/ArcMenu" in CONTAINERFILE
    assert "COPY --from=gnome-ext-build" in CONTAINERFILE
    # Its schema installs into the shared directory, which must be recompiled.
    assert "glib-compile-schemas /usr/share/glib-2.0/schemas" in CONTAINERFILE


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


# -- Hyprland and Umbriel ----------------------------------------------------------

def test_hyprland_comes_from_the_copr_which_is_removed_afterwards():
    assert "copr.fedorainfracloud.org/coprs/nett00n/hyprland/repo/fedora-44/" in CONTAINERFILE
    assert re.search(r'install "hyprland-\$\{HYPRLAND_VERSION\}\.\*"', CONTAINERFILE)
    assert "xdg-desktop-portal-hyprland" in CONTAINERFILE
    # Whether or not the install worked, the repository does not stay.
    assert re.search(r"; \\\n\s*rm -f /etc/yum.repos.d/kosher-hyprland.repo", CONTAINERFILE)
    # Its bundled wallpapers are anime drawings; they do not ship.
    assert "rm -f /usr/share/hypr/wall*.png" in CONTAINERFILE


def test_umbriel_is_built_from_pinned_commits_and_checked():
    for arg in ("UMBRIEL_REF", "UMBRIEL_PORTAL_REF"):
        assert re.search(rf"^ARG {arg}=[0-9a-f]{{40}}$", CONTAINERFILE, re.M), arg
    assert re.search(r"^FROM ghcr\.io/aareman/kosher-base-fedora:44-\d{8}@sha256:[0-9a-f]{64} as umbriel-build$",
                     CONTAINERFILE, re.M)
    assert "COPY --from=umbriel-build /out/ /tmp/umbriel/" in CONTAINERFILE
    script = (ROOT / "os-image/desktop/build-umbriel.sh").read_text()
    assert 'test "$(git -C "$umbriel" rev-parse HEAD)" = "$umbriel_ref"' in script
    assert 'test "$(git -C "$portal" rev-parse HEAD)" = "$portal_ref"' in script
    assert "set -euo pipefail" in script


def test_umbriels_own_starter_config_is_the_kosheros_one():
    # Umbriel's starter, which says to copy it to ~/.config, has no shell at
    # startup and none of the KosherOS keys; a person who copied it got a
    # black desktop with no help or settings keys. The image puts the
    # KosherOS config in its place, after os-image/files is in.
    files_in = CONTAINERFILE.index("COPY os-image/files/ /")
    over = CONTAINERFILE.index(
        "install -m 0644 /etc/xdg/umbriel/config.toml /usr/share/umbriel/config.toml")
    assert over > files_in
    assert "cmp /etc/xdg/umbriel/config.toml /usr/share/umbriel/config.toml" in CONTAINERFILE
    ours = (ROOT / "os-image/files/etc/xdg/umbriel/config.toml").read_text()
    for key in ('autostart = ["noctalia"]', '"Mod+Shift+Slash" = "cheatsheet-toggle"',
                '"Mod+Shift+Comma" = "spawn:noctalia msg settings-toggle"'):
        assert key in ours, key


def _hyprland_binds(text):
    """The key strings of every hl.bind, with the loops spelled out."""
    keys = re.findall(r'^hl\.bind\(([^,]+),', text, re.M)
    out = []
    for k in keys:
        k = k.strip()
        if k.startswith('"'):
            out.append(k.strip('"'))
        else:  # mainMod .. " + X"
            out.append("SUPER" + "".join(re.findall(r'"([^"]*)"', k)))
    for i in range(1, 10):
        out += [f"SUPER + {i}", f"SUPER + CTRL + {i}"]
    for direction, letter in (("left", "H"), ("down", "J"), ("up", "K"), ("right", "L")):
        out += [f"SUPER + {direction}", f"SUPER + {letter}", f"SUPER + CTRL + {direction}",
                f"SUPER + CTRL + {letter}", f"SUPER + SHIFT + {direction}"]
    return [re.sub(r"\s+", "", k).upper() for k in out]


def test_the_hyprland_session_starts_the_shell_and_the_session_target():
    text = HYPRLAND_CONFIG.read_text()
    start = re.search(r'hl\.on\("hyprland\.start", function\(\)(.*?)\nend\)', text, re.S).group(1)
    assert 'hl.exec_cmd("noctalia")' in start
    assert "systemctl --user import-environment WAYLAND_DISPLAY XDG_CURRENT_DESKTOP" in start
    assert "systemctl --user start kosher-hyprland-session.target" in start
    unit = (FILES / "usr/lib/systemd/user/kosher-hyprland-session.target").read_text()
    assert "BindsTo=graphical-session.target" in unit
    # What the target brings up: the helper that seeds Noctalia, and the
    # time-limit warnings.
    for name in ("kosher-layout.service", "kosher-time-notify.service"):
        assert "WantedBy=graphical-session.target" in (FILES / "usr/lib/systemd/user" / name).read_text()


def test_the_hyprland_keyboard_and_shell_binds():
    text = HYPRLAND_CONFIG.read_text()
    assert 'kb_layout  = "us,il"' in text
    assert 'kb_options = "grp:alt_shift_toggle"' in text
    assert 'ipc .. "session lock"' in text and 'ipc .. "panel-toggle launcher"' in text
    assert 'local ipc      = "noctalia msg "' in text
    assert '"switch:on:Lid Switch"' in text
    assert "force_default_wallpaper  = 0" in text and "disable_hyprland_logo    = true" in text
    binds = _hyprland_binds(text)
    dupes = {k for k in binds if binds.count(k) > 1}
    assert not dupes, dupes


def test_the_umbriel_session_starts_the_shell_with_the_same_keys():
    doc = tomllib.loads(UMBRIEL_CONFIG.read_text())  # a repeated key fails here
    assert doc["general"]["autostart"] == ["noctalia"]
    assert doc["events"]["lid_close"] == "noctalia msg session lock"
    assert doc["input"]["keyboard"]["layout"] == "us,il"
    assert doc["input"]["keyboard"]["options"] == "grp:alt_shift_toggle"
    binds = doc["keybinds"]
    assert binds["Mod+Space"] == "spawn:noctalia msg panel-toggle launcher"
    assert binds["Mod+Escape"] == "spawn:noctalia msg session lock"
    assert binds["Mod+Shift+E"] == "session-quit"


def test_the_three_compositors_share_the_main_keys():
    niri = NIRI_CONFIG.read_text()
    hypr = HYPRLAND_CONFIG.read_text()
    umbriel = tomllib.loads(UMBRIEL_CONFIG.read_text())["keybinds"]
    assert 'Mod+T hotkey-overlay-title="Open a Terminal" { spawn "ptyxis"; }' in niri
    assert 'hl.bind(mainMod .. " + T",             hl.dsp.exec_cmd(terminal))' in hypr
    assert umbriel["Mod+T"]["action"] == "spawn:ptyxis"
    assert 'Mod+Q repeat=false { close-window; }' in niri
    assert 'hl.bind(mainMod .. " + Q",         hl.dsp.window.close())' in hypr
    assert umbriel["Mod+Q"]["action"] == "window-close"


def test_the_noctalia_seed_parses_and_turns_the_polkit_agent_on():
    doc = tomllib.loads(NOCTALIA_SEED.read_text())
    # Without an agent the admin app's password prompt has nowhere to
    # appear in this session, and every privileged action just fails.
    assert doc["shell"]["polkit_agent"] is True
    assert doc["shell"]["telemetry_enabled"] is False
    assert doc["wallpaper"]["directory"] == "/usr/share/backgrounds/kosheros"
    assert layout_mod.NOCTALIA_SEED == Path("/usr/share/kosher/desktop/noctalia.toml")


def test_the_advanced_desktop_wears_the_kosheros_colours():
    # Issue #95: a light theme built on the wallpaper, the default on the
    # Noctalia desktop, with the bundled palettes still there to pick.
    doc = tomllib.loads(NOCTALIA_SEED.read_text())
    theme = doc["theme"]
    assert theme["mode"] == "light"
    assert theme["source"] == "wallpaper"
    assert theme["wallpaper_scheme"] in ("m3-tonal-spot", "m3-content", "m3-fruit-salad",
                                         "m3-rainbow", "m3-monochrome", "vibrant",
                                         "faithful", "soft", "dysfunctional", "muted")
    assert theme["pure_black_dark"] is False
    # The palette is generated from the default wallpaper, so one has to
    # be declared, and it is the file the build writes.
    assert doc["wallpaper"]["default"]["path"] == "/usr/share/backgrounds/kosheros/kosheros.png"
    rasters = (ROOT / "branding/rasters.py").read_text()
    assert "usr/share/backgrounds/kosheros/kosheros.png" in rasters
    assert NOCTALIA_SEED.relative_to(FILES) == Path(str(layout_mod.NOCTALIA_SEED).lstrip("/"))


def test_the_wallpaper_directory_gets_a_raster_copy():
    # Noctalia is pointed at the directory; a PNG is what every shell draws.
    # The build runs branding/rasters.py, which writes it there.
    assert "python3 /usr/share/kosher/branding/rasters.py" in CONTAINERFILE
    rasters = (ROOT / "branding/rasters.py").read_text()
    assert "usr/share/backgrounds/kosheros/kosheros.png" in rasters


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
    assert '"SetMyLayout", "(s)"' in src
    xml = (ROOT / "kosherd/src/kosherd/daemon.py").read_text()
    assert '<method name="SetLayout">' in xml
    assert '<method name="GetMyLayout">' in xml
    assert '<method name="SetMyLayout">' in xml


def test_the_polkit_action_for_choosing_your_own_layout_needs_no_prompt():
    policy = (FILES / "usr/share/polkit-1/actions/org.kosherlinux.policy").read_text()
    block = re.search(r'<action id="org.kosherlinux.set-own-desktop">(.*?)</action>',
                      policy, re.S)
    assert block and "<allow_active>yes</allow_active>" in block.group(1)
    rules = (FILES / "etc/polkit-1/rules.d/49-kosher-admin.rules").read_text()
    assert "org.kosherlinux.set-own-desktop" in rules


def test_the_schema_copies_agree_on_the_layout():
    for path in (ROOT / "policy/schema/policy.schema.json",
                 ROOT / "kosherd/src/kosherd/data/policy.schema.json"):
        schema = json.loads(path.read_text())
        assert schema["$defs"]["user"]["properties"]["layout"]["enum"] == list(LAYOUTS)


def test_the_apps_icon_is_the_bais_hamikdosh_in_the_brand_blue():
    import importlib.util

    spec = importlib.util.spec_from_file_location("rasters", ROOT / "branding/rasters.py")
    rasters = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rasters)
    icon = rasters.home_icon(256)
    assert icon.size == (256, 256) and icon.mode == "RGBA"
    facade = icon.getpixel((60, 100))
    assert facade[3] == 255 and facade[2] > facade[0], "opaque, and blue"
    # The Ulam's entrance, 20 amos wide and 40 high, has no doors: open
    # above the Heichal's doorway, which stands 20 amos tall inside it.
    assert icon.getpixel((128, 150))[3] == 0, "the entrance is open"
    assert icon.getpixel((128, 200)) == rasters.BRAND_BLUE_DEEP, "the Heichal doorway"
    # The lowest beam above the entrance is an amah wider on each side
    # than the opening; the top one is five amos wider.
    assert icon.getpixel((110, 135)) == rasters.BRAND_BLUE_DEEP
    assert icon.getpixel((102, 135))[3] == 255 and icon.getpixel((102, 135)) != rasters.BRAND_BLUE_DEEP
    assert icon.getpixel((102, 111)) == rasters.BRAND_BLUE_DEEP
    # Steps below the facade, wider than it; nothing in the corners.
    assert icon.getpixel((10, 240))[3] == 255, "the bottom step"
    assert icon.getpixel((10, 200))[3] == 0, "beside the facade"
    assert icon.getpixel((8, 8))[3] == 0, "transparent corners"
    # Every size comes from the one description, down to the taskbar's.
    assert rasters.home_icon(24).size == (24, 24)


def test_gnome_never_asks_the_family_for_a_donation():
    # "remove gnome asking for a donation. weird pop up i just got." GNOME
    # Settings Daemon's housekeeping plugin shows the appeal twice a year;
    # off machine-wide, and locked so nothing turns it back on.
    cp = _dconf(DESKTOP_DCONF)
    assert cp["org/gnome/settings-daemon/plugins/housekeeping"]["donation-reminder-enabled"] == "false"
    locks = (FILES / "etc/dconf/db/local.d/locks/10-kosheros-locks").read_text()
    assert "/org/gnome/settings-daemon/plugins/housekeeping/donation-reminder-enabled" in locks
