# KosherOS branding (roadmap stage)

Primary brand: **KosherOS**. Secondary attribution: **powered by Fedora** —
this matches Fedora's trademark policy for derivatives ("Fedora Remix"), so
Fedora references don't need scrubbing; only primary branding changes.

## Surfaces, boot → desktop (in user-visible order)

| surface | mechanism | needs artwork? |
|---|---|---|
| OS identity (About dialog, `hostnamectl`, portal/device lists) | `/usr/lib/os-release` NAME/PRETTY_NAME/VARIANT (done, in Containerfile) | no |
| Boot splash | Plymouth theme (`/usr/share/plymouth/themes/kosheros/`, `plymouth-set-default-theme` + initramfs regen in image build) | logo SVG |
| GRUB menu (mostly hidden) | `GRUB_DISTRIBUTOR="KosherOS"` | no |
| Login screen | GDM logo (`org.gnome.login-screen` dconf key `logo`), optional background CSS | logo SVG (light/dark) |
| First-boot welcome wizard (stage 4) | our own GTK app — brand it from day one | logo + wordmark |
| Desktop defaults | dconf db: default + locked wallpaper, favorite apps | wallpaper(s) |
| Admin app | GTK/libadwaita app icon + header | app icon |
| ISO installer (stage 4) | Anaconda product name via bootc-image-builder / kickstart | logo |
| Portal web UI (stage 5) | shared asset set | logo + wordmark |

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
