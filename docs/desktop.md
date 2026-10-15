# The desktop: three layouts, one setting

KosherOS is for people who know Windows, a Mac or a Chromebook, and for the
relative who knows none of them. It is also for the one person in the
family who wants a tiling window manager. One per-account setting, the
**layout**, serves all of them. The person picks it for themselves in the
**Look** app, where the classic taskbar's look is tuned as well; it is not
a row in the admin app, though `kosherctl layout` can still set it for an
account from a terminal. Classic and tiling switch in place; the advanced
desktop is a different session and takes effect at the next sign-in.

| Layout | What it is | Who it is for |
|---|---|---|
| `classic` (default) | GNOME with a taskbar along the bottom, an **Apps** button that opens a Windows-style menu with pinned apps, an all-apps list and search, tray icons, minimise and maximise buttons, GNOME's dynamic workspaces, no hot corner, and no Activities overview at sign-in | almost everyone |
| `tiling` | GNOME as GNOME ships it, plus PaperWM's scrolling tiling; the same lock screen, GNOME Settings, portals and accessibility | keyboard-driven power users who still want a supported desktop |
| `advanced` | a separate login-screen session: the niri compositor with the Noctalia shell (bar, launcher, notifications, lock screen, control centre, polkit agent), configured by text files the user owns | people who already run a tiling compositor and want theirs |

Filtering is identical under all three. It is per account at the network
layer, so which program draws the windows cannot weaken it, and that is
why the layout is a preference the person chooses for themselves, with no
admin and no guardian password. The layout is not part of a group, because
a protection level does not imply a window manager, and the guest account
always gets the classic desktop.

## The Look app

Open to every account. It shows the three desktops with the current one
marked, and choosing another asks kosherd to record it for this account
(`SetMyLayout`, which takes the account from the D-Bus connection, so
nobody chooses for anybody else) and, for classic and tiling, applies it
to the running session at once through the sign-in helper. Advanced is a
separate session, so the app says it is ready at the next sign-in and
offers to sign out.

Under Classic, the app also has the taskbar: how see-through it is,
whether the wallpaper is blurred behind it, and its size, with a button
back to the KosherOS look. These go straight into the extensions' own
settings for the account, and the sign-in helper leaves them alone (it
writes its plan when the layout changes, not at every sign-in), so they
stick. Wallpaper, dark mode and text size stay in GNOME Settings, which
the app points to.

GNOME Settings has no way to add a panel, which is why these controls are
an app of their own rather than a section of its Appearance page. The
Look app is also the first piece of what may one day be a KosherOS
settings app in place of GNOME's.

## Why not write our own desktop

A desktop is not a panel and a launcher. It is a Wayland compositor, screen
lock, user switching, multi-monitor and fractional scaling, gestures, an
on-screen keyboard, a screen reader and magnifier, keyboard layouts and
right-to-left text, network, Bluetooth and printer settings, notifications,
a polkit agent, the Flatpak portals, idle and power management, every one
of which has to work for a grandmother. GNOME has the best accessibility
stack on Linux, and everything else here, the polkit agent the admin app
relies on, dconf locks, the login screen's ordering for first boot, the
portals Flatpak apps need, is built on it. A custom shell would take a year
or more to be less reliable than GNOME is today, and that year would come
out of the filter, which is the product. The pieces that are ours are small
and app-shaped: the sign-in helper, the admin app, and a planned
full-screen big-tiles launcher for approved-sites-only accounts.

## How it works

### The setting

The layout lives on each account's policy as an optional value defaulting
to `classic`, so existing policies are unchanged on disk. Setting it for
an account is a manage-users action with no guardian gate and no ruleset
render; setting one's own is a separate action every active local user
holds with no prompt, and the daemon takes the account from the bus. It also
tells accountsservice which session the login screen should preselect for
that account, so the person is never asked to find the gear menu. An
account can read its own layout through a polkit action any active local
user holds with no prompt; it is the one thing a non-admin may read about
themselves, and it says nothing about how they are filtered.

### The sign-in helper

A small helper runs as the person signing in, from a systemd user unit in
the graphical session. It asks the daemon for the layout, with bounded
retries and a fallback to classic, and writes that layout's GNOME settings
into the user's own database: the extension list, window buttons, hot
corners, workspaces. The plans are complete rather than diffs, so
switching an account back from tiling undoes everything tiling set.

Per-user writes rather than dconf locks, because locks are machine-wide and
the layout differs per account. The cost is that a person can change these
keys mid-session, and the price of that is a desktop they live with until
the next sign-in, when the helper puts it back. Keys that are the same for
everyone are locked: the login logo, and extension installation, which is
off.

Under niri the helper does one thing: copies the shipped Noctalia
configuration into the user's home if there is none yet, then never
touches it again. The seed carries the settings that matter: the polkit
agent on, without which a password prompt has nowhere to appear; the
KosherOS wallpaper; and a light theme whose colours Noctalia takes from
that wallpaper, so the advanced desktop looks like this machine. The
bundled palettes stay a click away in the shell's settings, and a choice
made there wins over the seed.

### The welcome

GNOME's "Welcome to GNOME" dialog, with its tour, is off: gnome-tour is
not in the image and the Shell's last-shown-version key is preset and
locked. In its place a KosherOS welcome opens the first time an account
signs in to GNOME, from an autostart entry, and leaves a stamp in the
account's config folder so it does not come back; "Welcome" in the Apps
menu opens it again. It asks kosherd the same question My Filter does, so
an administrator is told what the computer is, where KosherOS Admin, the
Store, Look and My Filter are, and what to do first, and a managed user is
told that their account is filtered, where their apps are, how to ask for
a blocked page, and how much time they have each day.

### The appearance

The classic taskbar uses a translucent navy background, rounded controls,
blue running-app indicators, and a cream Apps button. It sits flush with the
bottom and sides of the screen. These are editable defaults, not locked
settings.

Both GNOME layouts include an Aura Glass **shell preset**: its Quick Settings,
menus, notifications, and dialog styles over GNOME's base shell theme, tinted
to match KosherOS. The image builds Blur My Shell with Aura's patches from
pinned commits; it never runs a theme installer or downloads shell code at
login. Static wallpaper blur keeps the cost down. App-window blur is off,
and the notification background stays nearly opaque for readability.
GTK app themes, icons, fonts, the wallpaper, and the niri session retain their
existing defaults. This integrates Aura's shell styling, not its full personal
desktop installer, settings GUI, or update service.

The installed Dash to Panel version is seeded into the machine's dconf defaults
at image build, preventing the misleading update notice on an account's first
login. Genuine later updates may still display their notice.

The Store's Fonts category includes Nerd Fonts and standard font families.
Nix installs them per account; the desktop helper copies their font files into
that account's font directory so native and Flatpak apps can use them. It
refreshes the font cache after installation, upgrade, or removal and preserves
fonts the person added or changed themselves. Already-running apps may need
reopening to refresh their font lists.

### The image

- Fedora packages: Dash to Panel, AppIndicator, niri, Noctalia, and the
  GTK portal backend Flatpak apps need outside GNOME. The classic GNOME
  session is removed so the login screen offers exactly two sessions.
- ArcMenu is not packaged by Fedora and needs build tools, so a builder
  stage, pinned to a release, builds it and only the result is copied in.
  The button says "Apps" beside the front of the Bais Hamikdosh in the
  brand blue, because a picture alone did not tell anyone where the apps
  were, and the KosherOS mark is the admin app's icon and read as "admin"
  on the taskbar. If a build ships without ArcMenu, Dash to Panel's own
  apps button with the same picture is the fallback.
- **Hebrew out of the box.** Every account gets English first and Hebrew
  second, the login screen the same, and the advanced session reads the
  system keymap. Super+Space and Alt+Shift both switch, and the indicator
  appears in the panel and in Noctalia's bar. The Culmus family, Noto
  Sans, Serif and Rashi Hebrew, Ezra SIL, Alef and Plex Sans Hebrew are
  installed. A default, not a lock: adding Yiddish or Russian in Settings
  sticks.
- PaperWM is not packaged by Fedora, so a pinned release is cloned into
  the system extensions directory and its schema compiled in place. As a
  system extension no account can remove it and any account can be given
  it. The extension lists the shell versions it supports and the build
  checks for the current one.
- The classic layout is also the machine-wide dconf default, so a desktop
  is right before the helper has ever run.
- niri's system-wide fallback configuration is its default keymap with the
  shell wired in: Noctalia at startup, launcher, lock, control-centre and
  media keys through it, Ptyxis as the terminal, and an empty keyboard
  block so the layout comes from the system keymap, where the wizard and
  GNOME Settings put it. niri rejects a configuration with a key bound
  twice and falls back to built-in defaults that start programs the image
  does not have, so the unit tests check for duplicate binds.

### Extensions and GNOME upgrades

Extensions break on every GNOME upgrade on a rolling desktop. Not here: the
extensions ship inside the image, pinned to the GNOME in that image and
exercised by the same build. Users cannot install others and cannot
uninstall these. What remains is a job at every base-image bump: check
Dash to Panel, AppIndicator and PaperWM against the new shell version
before shipping.

## What the advanced layout does not give you yet

Noctalia covers the shell layer well: bar, dock, launcher, control centre,
notifications, lock screen, idle, on-screen displays, wallpaper, clipboard,
tray, polkit agent. The plumbing a full session needs is only partly there,
which is why this layout is opt-in and never a group's default:

- **Display and input configuration** are text, in niri's own
  configuration. GNOME Settings runs under niri but its Displays, Keyboard
  shortcuts and Multitasking panels are GNOME Shell specific.
- **Printers and Bluetooth pairing**: Noctalia has toggles; pairing and
  printer setup need GNOME Settings or standalone tools.
- **Accessibility**: niri has basic Orca support and AccessKit on its own
  UI; there is no on-screen keyboard, magnifier or high-contrast switch.
- **Churn**: Noctalia was rewritten from the ground up for its current
  major version, so expect configuration keys to move between releases.
  The seed uses only keys present in the shipped example.

Before it is offered as more than "advanced": the polkit prompt from the
admin app appears and works; a Flatpak app's file dialog opens; the lock
screen locks and unlocks; a Hebrew layout typed in GNOME Settings is active
under niri; all of it verified on the real image across two consecutive
base-image bumps. The advanced layout needs a real GPU and does not run in
a VM without one.

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

The unit tests cover the setting, the daemon, the helper's plans and the
image files; they cannot start a shell. Whether a desktop looks right is a
VM check, and a change here is not done until it has been seen there.
