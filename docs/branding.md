# KosherOS branding (roadmap stage)

Primary brand: **KosherOS**. Secondary attribution: **powered by Fedora** —
this matches Fedora's trademark policy for derivatives ("Fedora Remix"), so
Fedora references don't need scrubbing; only primary branding changes.

**Status:** the pipeline is built and wired into the image. The wallpaper
is real artwork (`branding/wallpaper.png`, 4978×3340, light sepia — the
reason the desktop, greeter and Noctalia all default to the light scheme;
its GIMP source `wallpaper.xcf` stays in the repo and out of the image).
The logo is `branding/logo.png` (1316×1088 with alpha, source `logo.xcf`);
the Containerfile scales it to 256 px for Plymouth and the classic
desktop's start button and to 64 px for the login screen, fitted in a
square with transparent padding. Replacing that file re-brands all three on
the next build. `kosheros-logo.svg` and `wallpaper.svg` are the earlier
placeholders, kept in the repo but no longer drawn anywhere except the
SVG wallpaper copy installed for anything still pointing at it.

Worth checking on a booted machine: the logo is a light, low-contrast
image, so at 64 px on the light login screen and at 48 px on the taskbar it
may read as a faint blob. If so, a bolder mark (a dark outline or a solid
version) for the small sizes is the fix — a second file, not a redesign.

## Surfaces, boot → desktop (in user-visible order)

| surface | mechanism | needs artwork? |
|---|---|---|
| OS identity (About dialog, `hostnamectl`, portal/device lists) | ✅ `/usr/lib/os-release` NAME/PRETTY_NAME/VARIANT | no |
| Boot splash | ✅ Plymouth script theme `kosheros` (navy gradient, pulsing logo, LUKS prompt) | real logo |
| GRUB menu (mostly hidden) | ✅ `GRUB_DISTRIBUTOR="KosherOS"` | no |
| Login screen | ✅ GDM logo + banner via locked dconf keys | real logo |
| First-boot welcome wizard (stage 5) | our own GTK app — brand it from day one | logo + wordmark |
| Desktop defaults | ✅ dconf: wallpaper (unlocked), light scheme, favourites incl. the Store, classic taskbar layout (see [desktop.md](desktop.md)) | ✅ `branding/wallpaper.png` (source: `wallpaper.xcf`, kept out of the image) |
| Admin app / Store | GTK/libadwaita app icons (stock icons today) | app icons |
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
