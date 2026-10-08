# Branding

The product is **KosherOS**, with **powered by Fedora** as the secondary
line. That matches Fedora's trademark policy for derivatives, a Fedora
Remix, so Fedora references do not need scrubbing; only the primary
branding changes.

## Two artwork files

Everything the family sees is made from two files in `branding/`:

- `wallpaper.png`, a light sepia painting with the wordmark in a corner.
  It is the reason the desktop, the login screen and the advanced session
  all default to the light scheme. Its GIMP source stays in the repository
  and out of the image.
- `logo.png`, the mark with transparency, and its GIMP source.

`branding/rasters.py` runs at image build and makes every raster from
those two:

- the boot-splash logo;
- a house in the brand blue for the classic desktop's Apps button, drawn
  rather than taken from the mark, because the mark is the admin app's
  icon and read as "admin" on the taskbar;
- the admin app's icon at every size, named `org.kosherlinux.Admin`. The
  one app that is the product carries the mark; the Store and Setup keep
  stock icons until they get their own;
- the login-screen lockup, the logo with "KosherOS" set beside it in an
  italic serif, because the login screen draws one image at native size
  and the mark alone read as a faint blob;
- the wallpaper plus a crop per screen shape (16:9, 16:10, 3:2, 4:3, 5:4,
  21:9), each anchored top-left so the wordmark survives, and the XML that
  lists them so GNOME picks the one matching the screen. GNOME's own zoom
  on a single picture cropped from the centre and cut the word off;
- the GRUB menu's background and theme.

Replacing either artwork file re-brands everything on the next build. If
the wallpaper is ever repainted, a wordmark kept about 10% in from every
edge would need none of the cropping care.

## Where the brand appears

| Surface | How |
|---|---|
| OS identity: the About dialog, `hostnamectl`, device lists | `/usr/lib/os-release` carries the name, pretty name and variant. `ID` stays `fedora` and `VERSION_ID` stays Fedora's, because tooling keys off them |
| Boot splash | a Plymouth theme: navy gradient, pulsing logo, the disk-password prompt |
| GRUB menu | a theme: navy, the mark and name, the entries, a countdown bar |
| Login screen | the lockup and a banner, through locked dconf keys |
| First-boot wizard | the project's own GTK app |
| Desktop defaults | wallpaper, light scheme, favourites including the Store, the classic taskbar layout; see [desktop](desktop.md) |
| Admin app | the mark as its icon |
| Installer | a product image injected onto the ISO; see below |

## The GRUB menu

The boot loader shows its menu for one second on every boot, which is long
enough to notice white-on-black text listing the OS. So the menu is drawn
from a theme, loaded by a configuration snippet that bootupd concatenates
into the boot configuration at install.

The theme has to be readable before the OS is up, and that differs by
firmware, so it is installed twice. For UEFI it lives under the EFI
component directory bootupd copies onto the EFI system partition, with its
own copy of the font. For BIOS it takes the name of the one theme
directory GRUB's installer copies onto the boot partition. If neither the
theme nor a font is found the snippet changes nothing and the plain menu is
shown. Under Secure Boot GRUB will not load modules from disk, but the
graphics, PNG and video modules are built into Fedora's signed loader, so
the theme works there too.

This has not yet been seen on a booted machine.

## The installer

The installer ISO comes out of the image builder wearing Fedora's branding,
which broke the illusion at the first thing a new owner sees. Anaconda has
a hook for this, and `just iso` uses it: after the ISO is written, a small
product image is built and placed on it, with the boot records replayed so
it still boots on BIOS and UEFI, and the result is named
`KosherOS-<version>-<arch>.iso`. The version comes from the git tag, which
is the version everywhere.

The installer's initramfs finds that image on the install media and copies
it over the installer's root before Anaconda starts, so it replaces three
things:

- the build stamp Anaconda reads its product name and version from, so
  the welcome screen says KosherOS;
- a configuration drop-in that points at the KosherOS stylesheet and
  hides every hub screen the kickstart already answers: keyboard,
  language, time zone, network, source, software. The disk question
  answers itself when the machine has exactly one internal drive that is
  not the installer stick, so the hub arrives complete and the install is
  a single Begin Installation button; with two or more candidate disks the
  installer asks, so a second drive is never wiped unseen;
- the stylesheet, a navy sidebar and top bar with the mark at the top of
  the sidebar, and the logo.

A unit test builds the archive, reads it back with the system cpio, and
rehearses the injection on a stand-in ISO. The branded installer has not
yet been seen booted; that needs `just iso` and `just boot-iso`, which
need sudo.

## Still to make

- a wordmark for the installer's top bar;
- icons for the Store and Setup;
- a dark variant of the wallpaper.
