"""The KosherOS apps run from a Nix terminal without loading Nix's GTK.

Ghostty from nixpkgs left GI_TYPELIB_PATH and its kin pointing at Nix's
GTK in every shell it opened, and kosher-admin and kosher-look then failed
with "version `GLIBC_2.44' not found". These are the variables from that
shell.
"""

from pathlib import Path

from kosherd.gtkenv import drop_nix

REPO = Path(__file__).parents[2]
APPS = ("admin-app/src/kosheradmin", "look-app/src/kosherlook",
        "store-app/src/kosherstore", "myfilter-app/src/koshermyfilter",
        "setup-app/src/koshersetup", "welcome-app/src/kosherwelcome")

GHOSTTY_SHELL = {
    "GI_TYPELIB_PATH": "/nix/store/7i0b-glib-2.88.3/lib/girepository-1.0:"
                       "/nix/store/98zp-gtk4-4.22.4/lib/girepository-1.0",
    "GIO_EXTRA_MODULES": "/nix/store/bdcr-dconf-0.49.0-lib/lib/gio/modules",
    "GDK_PIXBUF_MODULE_FILE": "/nix/store/8cqg-librsvg-2.62.3/lib/gdk-pixbuf-2.0/2.10.0/loaders.cache",
    "XDG_DATA_DIRS": "/nix/store/nh3q-ghostty-1.3.1/share:"
                     "/nix/store/98zp-gtk4-4.22.4/share/gsettings-schemas/gtk4-4.22.4:"
                     "/var/lib/flatpak/exports/share:/usr/local/share/:/usr/share/:"
                     "/var/home/abba/.nix-profile/share",
    "PATH": "/usr/bin:/nix/store/nh3q-ghostty-1.3.1/bin",
}


def test_nix_gtk_paths_are_dropped():
    env = dict(GHOSTTY_SHELL)
    changed = drop_nix(env, prefix="/usr")
    assert "GI_TYPELIB_PATH" not in env
    assert "GIO_EXTRA_MODULES" not in env
    assert "GDK_PIXBUF_MODULE_FILE" not in env
    assert sorted(changed) == ["GDK_PIXBUF_MODULE_FILE", "GIO_EXTRA_MODULES",
                               "GI_TYPELIB_PATH", "XDG_DATA_DIRS"]


def test_the_rest_of_a_list_stays_in_order():
    env = dict(GHOSTTY_SHELL)
    drop_nix(env, prefix="/usr")
    # The person's own Nix profile is theirs: its apps' icons and launchers.
    assert env["XDG_DATA_DIRS"] == ("/var/lib/flatpak/exports/share:/usr/local/share/:"
                                    "/usr/share/:/var/home/abba/.nix-profile/share")


def test_path_is_not_touched():
    env = dict(GHOSTTY_SHELL)
    drop_nix(env, prefix="/usr")
    assert env["PATH"] == GHOSTTY_SHELL["PATH"]


def test_a_clean_environment_is_left_alone():
    env = {"XDG_DATA_DIRS": "/usr/local/share/:/usr/share/", "GTK_PATH": "/usr/lib64/gtk-4.0"}
    assert drop_nix(env, prefix="/usr") == []
    assert env == {"XDG_DATA_DIRS": "/usr/local/share/:/usr/share/", "GTK_PATH": "/usr/lib64/gtk-4.0"}


def test_a_nix_python_keeps_nix_gtk():
    # The developer shell's Python, which the test suites run on.
    env = dict(GHOSTTY_SHELL)
    assert drop_nix(env, prefix="/nix/store/i3r8-python3-3.12.14-env") == []
    assert env == GHOSTTY_SHELL


def test_every_app_cleans_up_before_gtk():
    for app in APPS:
        init = (REPO / app / "__init__.py").read_text()
        assert "_gtkenv.drop_nix()" in init, f"{app} does not drop Nix's GTK paths"
        assert "import gi" not in init
