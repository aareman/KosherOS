#!/usr/bin/env python3
"""Brand the installer: make product.img and put it on the ISO.

The installer ISO comes out of bootc-image-builder looking like Fedora's,
which breaks the illusion at the very first thing a new owner sees. Anaconda
has a hook for exactly this: an `images/product.img` on the install media,
which the initramfs unpacks over the installer's root before Anaconda
starts (dracut's `anaconda_auto_updates`, then `apply-live-updates`). So a
small cpio archive can replace three things without rebuilding anything:

- `/.buildstamp`, where Anaconda reads the product name and version it
  prints ("WELCOME TO KOSHEROS 0.1");
- `/etc/anaconda/conf.d/90-kosheros.conf`, a drop-in loaded after the
  Fedora profile, pointing `custom_stylesheet` at ours;
- `/usr/share/anaconda/pixmaps/kosheros/`, the stylesheet and the logo it
  draws in the sidebar. Fedora's own stylesheet path is overwritten with
  the same file as a belt-and-braces, so the branding holds even if the
  drop-in were ever not read.

Run with the ISO path after `bootc-image-builder` has produced it; `just
iso` does. The cpio is written here in the `newc` format rather than by
calling cpio, so the dev shell needs nothing but Pillow and xorriso.
"""

from __future__ import annotations

import argparse
import gzip
import importlib.util
import io
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BRANDING = ROOT / "branding"

PRODUCT = "KosherOS"
# One version for the whole product, from the VERSION file at the repo root;
# the Containerfile puts the same number in os-release.
VERSION = (ROOT / "VERSION").read_text().strip()
BUG_URL = "https://github.com/aareman/KosherOS/issues"
# The deep navy of the boot splash (plymouth kosheros.script), so the
# installer, the splash and the login screen read as one product.
NAVY = "#0d1729"
PIXMAPS = "usr/share/anaconda/pixmaps/kosheros"
STYLESHEET = f"/{PIXMAPS}/kosheros.css"
# Where Fedora's profile points custom_stylesheet; overwritten with ours.
FEDORA_STYLESHEET = "usr/share/anaconda/pixmaps/fedora.css"
LOGO_SIZE = 150

CSS = f"""/* KosherOS installer theme: loaded by Anaconda as the custom stylesheet.
   Only what differs from Anaconda's own anaconda-gtk.css is here. */
@define-color kosheros_navy {NAVY};

/* The sidebar on every hub and spoke: navy, with the mark at the top. */
.logo-sidebar {{
    background-color: @kosheros_navy;
    background-image: none;
}}
.logo {{
    background-image: url('/{PIXMAPS}/sidebar-logo.png');
    background-position: 50% 24px;
    background-repeat: no-repeat;
    background-color: transparent;
}}
.product-logo {{
    background-image: none;
    background-color: transparent;
}}

/* The bar across the top with the product name and the step title. */
AnacondaSpokeWindow #nav-box {{
    background-color: @kosheros_navy;
    background-image: none;
    color: white;
}}
"""

# Anaconda's hub shows every step at once, in any order, and asks about
# things the kickstart in os-image/iso-config.toml has already answered
# (language, keyboard, time zone) or that an offline bootc install never
# needs (network, software, an installation source). A parent installing a
# family computer should see one question — which disk — and a button. So
# every spoke but Installation Destination is hidden; the Users module is
# already switched off in the ISO config, which removes the account and
# root-password spokes. Class names are Anaconda's GTK spoke classes.
HIDDEN_SPOKES = (
    "KeyboardSpoke",           # keyboard us, from the kickstart
    "LangsupportSpoke",        # lang en_US.UTF-8, from the kickstart
    "DatetimeSpoke",           # timezone, from the kickstart
    "NetworkSpoke",            # the install is offline; the OS brings the network up
    "SourceSpoke",             # the image is on the ISO
    "SoftwareSelectionSpoke",  # a bootc image has no package selection
)

ANACONDA_DROPIN = f"""# KosherOS: loaded after the Fedora profile (see /etc/anaconda/profile.d).
[User Interface]
custom_stylesheet = {STYLESHEET}
# One question, one button: everything the kickstart answers is hidden,
# leaving Installation Destination.
hidden_spokes =
""" + "".join(f"    {spoke}\n" for spoke in HIDDEN_SPOKES)


def buildstamp(version: str = VERSION, arch: str | None = None,
               now: float | None = None) -> str:
    """The [Main] section Anaconda reads its product name from."""
    arch = arch or os.uname().machine
    stamp = time.strftime("%Y%m%d%H%M", time.gmtime(now if now is not None
                                                     else time.time()))
    return (f"[Main]\nProduct={PRODUCT}\nVersion={version}\nBugURL={BUG_URL}\n"
            f"IsFinal=false\nUUID={stamp}.{arch}\nVariant={PRODUCT}\n"
            f"[Compose]\nLorax=kosheros-brand-iso\n")


def _rasters():
    spec = importlib.util.spec_from_file_location("rasters", BRANDING / "rasters.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def stage(out: Path, src: Path = BRANDING, version: str = VERSION) -> list[Path]:
    """Lay out product.img's contents under `out`, root-relative."""
    from PIL import Image

    made = []

    def write(rel: str, data: bytes) -> None:
        path = out / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        made.append(path)

    write(".buildstamp", buildstamp(version).encode())
    write("etc/anaconda/conf.d/90-kosheros.conf", ANACONDA_DROPIN.encode())
    write(f"{PIXMAPS}/kosheros.css", CSS.encode())
    write(FEDORA_STYLESHEET, CSS.encode())
    logo = Image.open(src / "logo.png").convert("RGBA")
    buffer = io.BytesIO()
    _rasters().fitted(logo, LOGO_SIZE).save(buffer, format="PNG")
    write(f"{PIXMAPS}/sidebar-logo.png", buffer.getvalue())
    return made


def cpio_newc(root: Path) -> bytes:
    """A `newc` cpio archive of everything under `root`, paths relative to it.

    Written by hand: it is a hundred lines of format and saves a tool
    dependency. Directories first, so the unpacker can create them, then
    files, then the trailer. Ownership is root, as it must be in an
    installer root.
    """
    out = io.BytesIO()
    ino = 1

    def entry(name: str, mode: int, data: bytes) -> None:
        nonlocal ino
        name_bytes = name.encode() + b"\0"
        header = ("070701" + f"{ino:08x}" + f"{mode:08x}" + "00000000" "00000000"
                  + f"{1:08x}" + f"{int(time.time()):08x}" + f"{len(data):08x}"
                  + "00000000" "00000000" "00000000" "00000000"
                  + f"{len(name_bytes):08x}" + "00000000")
        ino += 1
        out.write(header.encode())
        out.write(name_bytes)
        out.write(b"\0" * (-(110 + len(name_bytes)) % 4))
        out.write(data)
        out.write(b"\0" * (-len(data) % 4))

    for path in sorted(p for p in root.rglob("*") if p.is_dir()):
        entry(str(path.relative_to(root)), 0o040755, b"")
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        entry(str(path.relative_to(root)), 0o100644, path.read_bytes())
    entry("TRAILER!!!", 0, b"")
    return out.getvalue()


def product_img(dest: Path, src: Path = BRANDING, version: str = VERSION) -> Path:
    """Build product.img (gzip-compressed newc cpio) at `dest`."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "root"
        stage(root, src, version)
        archive = cpio_newc(root)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(gzip.compress(archive, mtime=0))
    return dest


def release_name(version: str = VERSION, now: float | None = None,
                 arch: str | None = None) -> str:
    """'KosherOS-0.1-20260910-x86_64.iso': what a person sees in Ventoy's
    menu or a downloads folder, and enough to tell two builds apart."""
    arch = arch or os.uname().machine
    day = time.strftime("%Y%m%d", time.localtime(now if now is not None
                                                  else time.time()))
    return f"{PRODUCT}-{version}-{day}-{arch}.iso"


def rename(iso: Path, version: str = VERSION) -> Path:
    """Give the ISO its release name and leave `install.iso` as a link to
    it, so the recipes that boot the ISO keep working unchanged."""
    named = iso.with_name(release_name(version))
    if named.exists():
        named.unlink()
    os.replace(iso, named)
    link = iso
    if link.is_symlink() or link.exists():
        link.unlink()
    link.symlink_to(named.name)
    return named


def _xorriso_runner(xorriso: str = "xorriso"):
    """How to call xorriso here.

    It is in the devenv shell, but a shell opened before it was added does
    not have it (which is exactly how the first branded build failed). With
    nix on the path, fetch it for this one run rather than stop.
    """
    if shutil.which(xorriso) is not None:
        prefix = [xorriso]
    elif shutil.which("nix") is not None:
        print("xorriso is not on the path; running it through nix "
              "(re-enter `devenv shell` to have it permanently)", file=sys.stderr)
        prefix = ["nix", "shell", "nixpkgs#xorriso", "-c", "xorriso"]
    else:
        raise SystemExit("xorriso is not installed: re-enter `devenv shell`, "
                         "which now provides it, and run `just brand-iso`")

    def run(args, **kwargs):
        return subprocess.run(prefix + args, check=True, **kwargs)

    return run


def inject(iso: Path, img: Path, xorriso: str = "xorriso") -> None:
    """Put product.img at images/product.img on the ISO, keeping it bootable.

    xorriso rewrites the image with the boot records replayed, so the
    result still boots on BIOS and UEFI. Written beside the original and
    swapped in only when xorriso succeeded.
    """
    run = _xorriso_runner(xorriso)
    branded = iso.with_suffix(".branded.iso")
    run(["-indev", str(iso), "-outdev", str(branded),
         "-boot_image", "any", "replay",
         "-map", str(img), "/images/product.img",
         "-end"], stdout=subprocess.DEVNULL)
    # -ls rather than -find: -find swallows every following word as its
    # own option, -end included, and fails.
    listing = run(["-indev", str(branded), "-ls", "/images", "-end"],
                  capture_output=True, text=True).stdout
    if "product.img" not in listing:
        branded.unlink(missing_ok=True)
        raise SystemExit("xorriso produced an ISO without images/product.img")
    os.replace(branded, iso)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("iso", nargs="?", type=Path,
                   help="the installer ISO to brand in place")
    p.add_argument("--product-img", type=Path,
                   help="where to write product.img (default: beside the ISO)")
    p.add_argument("--src", type=Path, default=BRANDING,
                   help="directory holding logo.png")
    p.add_argument("--version", default=VERSION)
    args = p.parse_args(argv)
    if args.iso is None and args.product_img is None:
        p.error("give an ISO to brand, or --product-img to only build the image")
    img = args.product_img or args.iso.with_name("product.img")
    product_img(img, args.src, args.version)
    print(f"product.img: {img}")
    if args.iso is not None:
        inject(args.iso, img)
        named = rename(args.iso, args.version)
        print(f"ISO: {named}  ({args.iso.name} points at it)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
