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
| GRUB menu (mostly hidden) | ✅ `GRUB_DISTRIBUTOR="KosherOS"` | no |
| Login screen | ✅ GDM logo + banner via locked dconf keys | real logo |
| First-boot welcome wizard (stage 5) | our own GTK app — brand it from day one | logo + wordmark |
| Desktop defaults | ✅ dconf: wallpaper (unlocked), light scheme, favourites incl. the Store, classic taskbar layout (see [desktop.md](desktop.md)) | ✅ `branding/wallpaper.png` (source: `wallpaper.xcf`, kept out of the image) |
| Admin app / Store | ✅ Admin: the KosherOS mark (hicolor, from `rasters.py`); Store and Setup still stock icons | Store + Setup icons |
| ISO installer | Anaconda branding: product name/version via `.buildstamp`, and a `product.img` overlaying the logo, sidebar and CSS in the installer's Anaconda theme | logo + wordmark |
| Portal web UI (stage 5) | shared asset set | logo + wordmark |

## Anaconda installer (do this once the brand is settled)

The installer currently shows stock Fedora branding, which breaks the
illusion at the very first thing a new owner sees. Anaconda takes branding
from two places, both of which bootc-image-builder can carry:

- **`/.buildstamp`** in the ISO root sets `Product`, `Version` and
  `BugURL`, which is what Anaconda prints as "Install <Product>";
- **`product.img`** — a small cpio/squashfs overlay mounted over the
  installer runtime — replaces the Anaconda theme under
  `/usr/share/anaconda/pixmaps/` (sidebar, logo, topbar background) and
  `/usr/share/anaconda/anaconda-gtk.css` for colours.

So the work is: generate those pixmaps from the final logo/wordmark at the
sizes Anaconda expects, build `product.img`, and have `just iso` inject it.
Deliberately deferred until the artwork is real — regenerating placeholder
installer graphics twice is wasted effort.

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
