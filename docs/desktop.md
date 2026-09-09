# The desktop: three layouts, one setting

KosherOS is for people who know Windows, a Mac or a Chromebook, and for the
relative who knows none of them. It is also for the one person in the
family who wants a tiling window manager. One per-account setting, the
**layout**, serves all of them; a parent picks it in the admin app
(Account tab → Desktop → Layout) or with `kosherctl layout <uid> <layout>`,
and it takes effect at that account's next sign-in.

| layout | what it is | who it is for |
|---|---|---|
| `classic` (default) | GNOME with a taskbar along the bottom (Dash to Panel), an **Apps** button (ArcMenu: the KosherOS mark plus the word, on a filled pill, opening a Windows-style menu with pinned apps, an all-apps list and search), tray icons, minimise/maximise buttons, one workspace, no hot corner, no Activities overview at sign-in | almost everyone |
| `tiling` | GNOME as GNOME ships it, plus PaperWM's scrolling tiling; same lock screen, GNOME Settings, portals and accessibility | keyboard-driven power users who still want a supported desktop |
| `advanced` | a separate login-screen session: the niri compositor with the Noctalia shell (bar, launcher, notifications, lock screen, control centre, polkit agent), configured by text files the user owns | people who already run a tiling compositor and want theirs |

Filtering is identical under all three. It is per-uid at the network layer
(nftables, dnsmasq, the proxy), so which program draws the windows cannot
weaken it; that is also why changing the layout is an admin action with no
guardian password. The layout is deliberately **not** part of the presets
(a protection level does not imply a window manager) and the guest account
always gets the classic desktop.

## Why not write our own desktop

A desktop is not a panel and a launcher. It is a Wayland compositor, screen
lock, user switching, multi-monitor and fractional scaling, gestures, an
on-screen keyboard, a screen reader and magnifier, keyboard layouts and
right-to-left text, network and Bluetooth and printer UI, notifications, a
polkit agent, the Flatpak portals, idle and power management — every one of
which has to work for the "grandma" target. GNOME has the best accessibility
stack on Linux and everything else here (the polkit agent the admin app
relies on, dconf locks, GDM ordering for first boot, the portals Flatpak
apps need) is built on it. A custom shell would take a year or more to be
less reliable than GNOME is today, and that year would come out of the
filter, which is the product. The pieces that ARE ours are small and
app-shaped: the sign-in helper, the admin app, and (planned) a full-screen
big-tiles launcher for the young-child preset on top of gnome-kiosk.

## How it works

### The setting

`layout` lives on each `UserPolicy` (kosherd/policy.py, `LAYOUTS`), in the
schema as an optional enum defaulting to `classic`, so existing policies are
unchanged on disk. Two D-Bus methods on `Profiles`:

- `SetLayout(uid, layout)` — `manage-users` polkit action, no guardian gate,
  saved with `_save_only()` (no ruleset render, no proxy restart). It also
  tells accountsservice which session the login screen should preselect for
  that account (`gnome` for classic and tiling, `niri` for advanced), so the
  person is never asked to find the gear menu.
- `GetMyLayout()` — the caller's own layout, under a new polkit action
  `org.kosherlinux.read-own-settings` that any active local user holds with
  no prompt. It is the one thing a non-admin may read about themselves, and
  it says nothing about how they are filtered.

### The sign-in helper

`kosher-layout` (kosherd/layout.py) runs as the person signing in, from the
systemd user unit `kosher-layout.service` wanted by
`graphical-session.target`. It asks kosherd for the layout (bounded retries,
falls back to classic) and writes that layout's GNOME settings with
`gsettings` into the user's own database: the extension list, window
buttons, hot corners, workspaces. The plans are complete rather than diffs,
so switching an account back from tiling undoes everything tiling set.

Why per-user writes rather than dconf locks: locks are machine-wide, and
the layout differs per account. The cost is that a person can change these
keys mid-session; the price of that is a desktop they live with until the
next sign-in, when the helper puts it back. Keys that are the same for
everyone are locked (the login logo; extension installation, off).

Under niri the helper does one thing: copies
`/usr/share/kosher/desktop/noctalia.toml` to `~/.config/noctalia/config.toml`
if that does not exist yet, then never touches it again. Noctalia has no
system-wide configuration layer, and the seed carries the two settings that
matter — the polkit agent on (without it the admin app's password prompts
have nowhere to appear) and the KosherOS wallpaper.

### The image

- Fedora packages: `gnome-shell-extension-dash-to-panel`,
  `gnome-shell-extension-appindicator`, `niri`, `noctalia`,
  `xdg-desktop-portal-gtk` (the portal backend Flatpak apps need outside
  GNOME; the GNOME portal and gnome-keyring come with the workstation
  group). `gnome-classic-session` is removed so the login screen offers
  exactly two sessions.
- ArcMenu is not packaged by Fedora either and needs `glib-compile-resources`
  and gettext to build, so a builder stage (`gnome-ext-build`, pinned by
  `ARCMENU_REF`) builds it and only the result is copied in. Its schema
  installs into the shared schema directory, which the build recompiles. A
  picture alone did not tell anyone where the apps were, so the button says
  "Apps"; if a build ships without ArcMenu, Dash to Panel's own apps button
  with the KosherOS mark is the fallback.
- **Hebrew out of the box.** Every account gets English first and Hebrew
  second (`30-kosheros-input`), the login screen the same (`gdm.d`), and the
  advanced session reads `us,il` from the system keymap in
  `/etc/X11/xorg.conf.d/00-keyboard.conf` via systemd-localed. Super+Space
  and Alt+Shift both switch; the indicator appears in the panel, and in
  Noctalia's bar as the `keyboard_layout` widget. The Culmus family, Noto
  Sans/Serif/Rashi Hebrew, Ezra SIL, Alef and Plex Sans Hebrew are
  installed. A default, not a lock: adding Yiddish or Russian in Settings
  sticks.
- PaperWM is not packaged by Fedora, so a pinned release (`PAPERWM_REF`) is
  cloned into `/usr/share/gnome-shell/extensions/paperwm@paperwm.github.com`
  and its schema compiled in place. As a system extension under `/usr` no
  account can remove it and any account can be given it. Bump the ref when
  the base image moves to a new GNOME; the extension's `metadata.json`
  lists supported shell versions and the build checks for the current one.
- `/etc/dconf/db/local.d/20-kosheros-desktop` is the classic layout as the
  machine-wide default, so a desktop is right before the helper has ever
  run. Dash to Panel is told not to open the Activities overview at sign-in
  and to use the KosherOS logo as its start button.
- `/etc/niri/config.kdl` is niri's system-wide fallback (used until an
  account writes `~/.config/niri/config.kdl`; copy it there to customise).
  It is niri's default keymap with the shell wired in: `spawn-at-startup
  "noctalia"`, launcher/lock/control-centre/media keys through `noctalia
  msg`, Ptyxis as the terminal, an empty `xkb {}` so the keyboard layout
  comes from `localectl` (where the wizard and GNOME Settings put it — so
  Hebrew is there without editing a file). niri rejects a config with a
  key bound twice and falls back to built-in defaults that start programs
  the image does not have; the unit tests check for duplicate binds.

### The usual objection to extensions

"Extensions break on every GNOME upgrade." True on a rolling desktop; not
here. The extensions ship inside the bootc image, pinned to the GNOME in
that image and exercised by the same build. Users cannot install others
(`allow-extension-installation` is locked off) and cannot uninstall these.
What remains is our job at every base-image bump: check Dash to Panel,
AppIndicator and PaperWM against the new shell version before shipping.

## What the advanced layout does not give you (yet)

Noctalia covers the shell layer well: bar, dock, launcher, control centre,
notifications, lock screen, idle, OSDs, wallpaper, clipboard, tray, polkit
agent. The plumbing a full session needs is only partly there, which is
why this layout is opt-in and never a preset default:

- **Display and input configuration** are text (niri's `output` and `input`
  blocks); GNOME Settings runs under niri but its Displays, Keyboard
  shortcuts and Multitasking panels are GNOME Shell specific.
- **Printers and Bluetooth pairing**: Noctalia has toggles, pairing and
  printer setup need GNOME Settings or standalone tools.
- **Accessibility**: niri has basic Orca support (`Super+Alt+S`) and
  AccessKit on its own UI; there is no on-screen keyboard, magnifier or
  high-contrast switch.
- **Churn**: Noctalia 5 reached stable in September 2026 after a ground-up
  rewrite; expect config keys to move between releases. The seed uses only
  keys present in the shipped `example.toml`.

Acceptance list for promoting it beyond "advanced": the polkit prompt from
the admin app appears and works; a Flatpak app's file dialog opens (portal);
the lock screen locks and unlocks; a Hebrew layout typed in GNOME Settings
is active under niri; all of it verified on the real image (see
[testing.md](testing.md)) across two consecutive base-image bumps.

## Verifying on a machine

```sh
kosherctl layout 1001 tiling          # as an admin
# sign out and back in as uid 1001, then as that user:
kosher-layout --dry-run               # what the helper writes
gsettings get org.gnome.shell enabled-extensions
gnome-extensions list --enabled
journalctl --user -u kosher-layout    # what it did at sign-in
# advanced:
niri validate -c /etc/niri/config.kdl
noctalia msg --help
```

The unit tests (`kosherd/tests/test_layout.py`) cover the setting, the
daemon, the helper's plans and the image files; they cannot start a shell.
Whether a desktop actually looks right is a VM check, and a change here is
not done until it has been seen there.
