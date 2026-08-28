# KosherOS branding (roadmap stage)

Primary brand: **KosherOS**. Secondary attribution: **powered by Fedora** —
this matches Fedora's trademark policy for derivatives ("Fedora Remix"), so
Fedora references don't need scrubbing; only primary branding changes.

**Status:** the pipeline is built and wired into the image; the artwork in
`branding/` is a placeholder logomark and wallpaper. Dropping real artwork
into `branding/kosheros-logo.svg` (256×256 viewBox) and
`branding/wallpaper.svg` re-brands every surface below on the next build —
no other changes needed.

## Surfaces, boot → desktop (in user-visible order)

| surface | mechanism | needs artwork? |
|---|---|---|
| OS identity (About dialog, `hostnamectl`, portal/device lists) | ✅ `/usr/lib/os-release` NAME/PRETTY_NAME/VARIANT | no |
| Boot splash | ✅ Plymouth script theme `kosheros` (navy gradient, pulsing logo, LUKS prompt) | real logo |
| GRUB menu (mostly hidden) | ✅ `GRUB_DISTRIBUTOR="KosherOS"` | no |
| Login screen | ✅ GDM logo + banner via locked dconf keys | real logo |
| First-boot welcome wizard (stage 5) | our own GTK app — brand it from day one | logo + wordmark |
| Desktop defaults | ✅ dconf: wallpaper (unlocked), dark scheme, favourites incl. the Store | real wallpaper |
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
