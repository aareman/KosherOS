# KosherOS branding (roadmap stage)

Primary brand: **KosherOS**. Secondary attribution: **powered by Fedora** —
this matches Fedora's trademark policy for derivatives ("Fedora Remix"), so
Fedora references don't need scrubbing; only primary branding changes.

**Status:** the pipeline is built and wired into the image. The wallpaper
is real artwork (`branding/wallpaper.png`, 4978×3340, light sepia — the
reason the desktop, greeter and Noctalia all default to the light scheme;
its GIMP source `wallpaper.xcf` stays in the repo and out of the image).
The logo is `branding/logo.png` (1316×1088 with alpha, source `logo.xcf`).
`branding/rasters.py`, run at image build, makes every raster from the two
artwork files:

- the boot-splash logo and the classic desktop's Apps button (256 px,
  fitted in a square), and the admin app's icon in hicolor at 48–512 px,
  named `org.kosherlinux.Admin` — the one app that IS the product carries
  the mark; the Store and Setup keep stock icons until they get their own;
- the **login-screen lockup**: the logo with "KosherOS" set beside it in an
  italic serif (Noto Serif in the image), because GDM's logo key is one
  image drawn at native size and the ellipse alone read as a faint blob;
- the wallpaper plus a **crop per screen shape** (16:9, 16:10, 3:2, 4:3,
  5:4, 21:9), each anchored top-left so the wordmark painted into the
  corner survives, and `kosheros.xml`, which lists them so gnome-bg picks
  the one matching the screen. GNOME's zoom on the single 3:2 picture
  cropped from the centre and cut the word off on 16:9 and 4:3 screens.

Replacing either artwork file re-brands everything on the next build. If
the wallpaper is ever repainted, a wordmark kept ~10% in from every edge
would need none of the cropping care. `kosheros-logo.svg` and
`wallpaper.svg` are the earlier placeholders, kept in the repo but no longer
drawn anywhere except the SVG wallpaper copy installed for anything still
pointing at it.

## Surfaces, boot → desktop (in user-visible order)

| surface | mechanism | needs artwork? |
|---|---|---|
| OS identity (About dialog, `hostnamectl`, portal/device lists) | ✅ `/usr/lib/os-release` NAME/PRETTY_NAME/VARIANT | no |
| Boot splash | ✅ Plymouth script theme `kosheros` (navy gradient, pulsing logo, LUKS prompt) | real logo |
| GRUB menu (one second, every boot) | ✅ a GRUB theme: navy, the mark and name, the entries, a countdown bar (see below) | no |
| Login screen | ✅ GDM logo + banner via locked dconf keys | real logo |
| First-boot welcome wizard (stage 5) | our own GTK app — brand it from day one | logo + wordmark |
| Desktop defaults | ✅ dconf: wallpaper (unlocked), light scheme, favourites incl. the Store, classic taskbar layout (see [desktop.md](desktop.md)) | ✅ `branding/wallpaper.png` (source: `wallpaper.xcf`, kept out of the image) |
| Admin app / Store | ✅ Admin: the KosherOS mark (hicolor, from `rasters.py`); Store and Setup still stock icons | Store + Setup icons |
| ISO installer | ✅ `product.img` injected by `just iso`: `.buildstamp`, a conf.d drop-in, our stylesheet and logo (see below) | wordmark for the top bar, eventually |
| Portal web UI (stage 5) | shared asset set | logo + wordmark |

## The GRUB menu

bootupd shows GRUB's menu for one second on every boot (`timeout_style=menu`
in its static config), which is long enough to notice white-on-black text
listing "KosherOS 0.1 (powered by Fedora 44) (ostree:0)". So the menu is
drawn from a theme: `branding/rasters.py` writes `theme.txt` and a navy
1920×1080 background with the lockup, and
`os-image/files/usr/lib/bootupd/grub2-static/configs.d/09_kosheros_theme.cfg`
— concatenated into grub.cfg by bootupd at install — loads the font, switches
to `gfxterm` and sets `theme`.

The theme has to be readable before the OS is up, and that differs by
firmware, so it is written twice:

- **UEFI**: under `/usr/lib/efi/kosheros-grub-theme/<version>/EFI/fedora/kosheros/`,
  with its own copy of `unicode.pf2`. bootupd treats every
  `/usr/lib/efi/<component>/<version>/EFI` tree as a component and copies it
  onto the EFI system partition; the Containerfile re-runs
  `bootupctl backend generate-update-metadata` so it is listed. GRUB finds
  it as `${cmdpath}/kosheros/`.
- **BIOS**: under `/usr/share/grub/themes/starfield/`. grub2-install copies
  exactly one theme directory onto the boot partition and by default it is
  the one named `starfield`; Fedora ships none, so the KosherOS theme takes
  the name. GRUB finds it as `${prefix}/themes/starfield/`, and the font at
  `${prefix}/fonts/unicode.pf2`, which grub2-install also copies.

If neither the theme nor a font is found the snippet changes nothing and
the plain menu is shown. Under Secure Boot GRUB will not load modules from
disk, but `gfxterm`, `png` and `all_video` are built into Fedora's signed
`grubx64.efi`, so the theme works there too. **Not yet seen on a booted
machine.**

## Anaconda installer

The installer ISO comes out of bootc-image-builder wearing Fedora's
branding, which broke the illusion at the very first thing a new owner
sees. Anaconda has a hook for exactly this, and `just iso` now uses it:
after bib writes `install.iso`, `scripts/brand-iso.py` builds a small
`product.img` and puts it at `images/product.img` on the ISO with xorriso
(boot records replayed, so it still boots on BIOS and UEFI), then names the
result `KosherOS-<version>-<date>-<arch>.iso`, leaving `install.iso` as a
link to it. The version comes from the `VERSION` file at the repo root,
which the Containerfile also puts in os-release.

How it takes effect, all of it stock Anaconda and verified against its
source: the initramfs finds `images/product.img` on the install media and
unpacks it into `/updates` (`dracut/anaconda-lib.sh`,
`anaconda_auto_updates`), and dracut's `apply-live-updates` copies that
over the installer's root before Anaconda starts. So the archive replaces
three things:

- `/.buildstamp` — where Anaconda reads its product name and version
  (`pyanaconda/core/product.py`), so the welcome screen says KosherOS;
- `/etc/anaconda/conf.d/90-kosheros.conf` — a drop-in loaded after the
  Fedora profile (`set_from_files`), pointing `custom_stylesheet` at ours
  and hiding every hub spoke the kickstart already answers (keyboard,
  language, time zone, network, source, software), so the installer is one
  question — which disk — and a Begin Installation button. The first real
  install found the stock hub "non-linear and quite complex"; this is the
  fix that stays inside Anaconda;
- `/usr/share/anaconda/pixmaps/kosheros/` — the stylesheet (navy sidebar
  and top bar, the mark at the top of the sidebar) and the logo, fitted
  from `branding/logo.png`. Fedora's own stylesheet path is overwritten
  with the same file as a belt-and-braces.

The cpio is written by the script itself (newc format, root-owned), so the
dev shell needs only Pillow and xorriso. `kosherd/tests/test_installer_branding.py`
builds the archive, reads it back with the system cpio, and rehearses the
xorriso injection on a stand-in ISO. **Not yet seen on a booted installer**:
that needs `just iso` and `just boot-iso`, which need sudo.

| surface | mechanism | needs artwork? |
|---|---|---|
| ISO installer | ✅ `product.img` on the ISO: `.buildstamp`, a conf.d drop-in, our stylesheet and logo | wordmark for the top bar, eventually |

## Asset checklist (create once, SVG-first)

- KosherOS logomark (symbol) — light + dark variants
- Wordmark ("KosherOS") and lockup with "powered by Fedora" line
- Plymouth splash animation (can start as static logo on dark background)
- Default wallpaper (light/dark)
- App icon for the admin app (hicolor + symbolic)

## Notes

- Keep `ID=fedora` and `VERSION_ID` in os-release — tooling (dnf repos,
  bootc, scripts) keys off `ID`; only NAME/PRETTY_NAME/VARIANT carry brand.
- dconf branding lives in `/etc/dconf/db/local.d/` + locks; ship in
  os-image/files once assets exist.
