#!/usr/bin/env python3
"""Every raster the image needs, from the two pieces of artwork.

Run at image build (the Containerfile copies branding/ to
/usr/share/kosher/branding and runs this); run locally with --out to see
what it makes. Pillow only — it is already in the image for the picture
filter.

From branding/logo.png:
  - the boot splash logo (256 px; the start button is a house of its own,
    fitted in a square, transparent padding);
  - the login-screen lockup: the logo with the word "KosherOS" beside it.
    GDM's logo key takes one image and draws it at native size, and a
    faint ellipse on its own said nothing — the name has to be in the
    picture.

From branding/wallpaper.png (3:2, wordmark in the top-left corner):
  - crops for the screen shapes people actually have, each anchored at the
    TOP-LEFT so the wordmark survives. GNOME's default "zoom" crops from the
    centre to fill the screen, which on a 16:9 or 4:3 screen cut the word
    off; a background XML lists the variants and gnome-bg picks the one
    whose shape matches the screen, which is how Fedora ships its own
    wallpapers.
"""

from __future__ import annotations

import argparse
import glob
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

# Italic serif, to match the wordmark painted into the wallpaper. First one
# found wins; the image has Noto Serif and Liberation Serif.
FONT_CANDIDATES = (
    "/usr/share/fonts/google-noto-vf/NotoSerif-Italic[wght].ttf",
    "/usr/share/fonts/google-noto/NotoSerif-Italic.ttf",
    "/usr/share/fonts/liberation-serif-fonts/LiberationSerif-Italic.ttf",
    "/usr/share/fonts/**/DejaVuSerif-Italic.ttf",
    "/usr/share/fonts/**/PTF56F.ttf",  # PT Serif Italic
    "/usr/share/fonts/**/*Serif*Italic*.ttf",
    "/usr/share/fonts/**/*.ttf",
)
WORDMARK = "KosherOS"
# The warm dark grey of the wordmark on the wallpaper; legible on GDM's
# light greeter.
WORDMARK_COLOUR = (72, 66, 58, 255)

# (width, height) aspect pairs, and the resolution to advertise each as in
# the XML. gnome-bg picks the entry whose aspect is nearest the screen's.
ASPECTS = (
    ((16, 9), (1920, 1080)),
    ((16, 10), (1920, 1200)),
    ((3, 2), (1920, 1280)),
    ((4, 3), (1600, 1200)),
    ((5, 4), (1280, 1024)),
    ((21, 9), (2560, 1080)),
)


def find_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for pattern in FONT_CANDIDATES:
        for path in sorted(glob.glob(pattern, recursive=True)):
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    return ImageFont.load_default(size)


def fitted(src: Image.Image, size: int) -> Image.Image:
    """The logo inside a size×size transparent square, aspect kept."""
    im = src.copy()
    im.thumbnail((size, size), Image.LANCZOS)
    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    canvas.paste(im, ((size - im.width) // 2, (size - im.height) // 2))
    return canvas


# The brand blue, from the logo's shield: light at the top, deep below.
BRAND_BLUE_TOP = (59, 130, 246, 255)
BRAND_BLUE_BOTTOM = (29, 78, 216, 255)


def home_icon(size: int = 256) -> Image.Image:
    """A house in the brand blue: the taskbar's Apps button.

    The KosherOS mark is the admin app's own icon, and the same mark on
    the taskbar said "admin" rather than "your apps" — the user asked for
    "just a home / house icon for the apps", in the branding colour. Drawn
    here rather than shipped as a bitmap so every size comes from one
    description: a roof, a body with a doorway cut out, a chimney, filled
    with the logo's own gradient.
    """
    s = size / 256
    mask = Image.new("L", (size, size), 0)
    draw = ImageDraw.Draw(mask)
    draw.polygon([(128 * s, 34 * s), (16 * s, 134 * s), (240 * s, 134 * s)], fill=255)
    draw.rounded_rectangle([52 * s, 118 * s, 204 * s, 224 * s], radius=14 * s, fill=255)
    draw.rounded_rectangle([170 * s, 52 * s, 198 * s, 112 * s], radius=6 * s, fill=255)
    draw.rounded_rectangle([104 * s, 158 * s, 152 * s, 224 * s], radius=8 * s, fill=0)
    fill = Image.new("RGBA", (size, size), BRAND_BLUE_BOTTOM)
    top, bottom = BRAND_BLUE_TOP, BRAND_BLUE_BOTTOM
    for y in range(size):
        t = y / max(1, size - 1)
        colour = tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(4))
        ImageDraw.Draw(fill).line([(0, y), (size, y)], fill=colour)
    icon = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    icon.paste(fill, (0, 0), mask)
    return icon


def lockup(src: Image.Image, height: int = 72, gap: int = 14,
           colour: tuple = WORDMARK_COLOUR) -> Image.Image:
    """Logo, then the name, on one transparent strip `height` tall."""
    logo = fitted(src, height)
    font = find_font(int(height * 0.62))
    probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    left, top, right, bottom = probe.textbbox((0, 0), WORDMARK, font=font)
    text_w, text_h = right - left, bottom - top
    canvas = Image.new("RGBA", (height + gap + text_w + 4, height), (0, 0, 0, 0))
    canvas.paste(logo, (0, 0), logo)
    draw = ImageDraw.Draw(canvas)
    draw.text((height + gap - left, (height - text_h) // 2 - top), WORDMARK,
              font=font, fill=colour)
    return canvas


def top_left_crop(src: Image.Image, aspect: tuple[int, int]) -> Image.Image:
    """The largest `aspect` crop of `src` that keeps its top-left corner."""
    aw, ah = aspect
    w, h = src.size
    if w * ah > h * aw:          # source is wider than the target: trim the right
        return src.crop((0, 0, h * aw // ah, h))
    return src.crop((0, 0, w, w * ah // aw))  # taller: trim the bottom


# --- the GRUB menu ------------------------------------------------------------
# The deep navy of the boot splash, so the menu, the splash and the installer
# read as one product. GRUB draws the menu itself, from a theme.txt and a
# background it can reach at boot; see 09_kosheros_theme.cfg in
# os-image/files/usr/lib/bootupd/grub2-static/configs.d for how it finds them.
GRUB_NAVY = (13, 23, 41, 255)
GRUB_NAVY_HEX = "#0d1729"
GRUB_TEXT = (201, 209, 224, 255)
GRUB_SIZE = (1920, 1080)
# grub2-install copies exactly one theme directory from /usr/share/grub/themes
# onto the boot partition, and by default that directory is named
# "starfield". Fedora ships no theme of that name, so the KosherOS theme
# takes the name and the BIOS boot path gets it for free.
GRUB_THEME_DIR = "usr/share/grub/themes/starfield"
# bootupd copies every /usr/lib/efi/<component>/<version>/EFI tree onto the
# EFI system partition, which is where UEFI GRUB can read it ($cmdpath).
GRUB_EFI_COMPONENT = "usr/lib/efi/kosheros-grub-theme/{version}/EFI/fedora/kosheros"
GRUB_FONT_SOURCES = ("/usr/share/grub/unicode.pf2",)

GRUB_THEME = f"""# KosherOS GRUB theme. Drawn by GRUB itself, so only what GRUB's theme
# language offers: a background, the menu, a label, the countdown.
desktop-image: "background.png"
desktop-image-scale-method: "crop"
desktop-color: "{GRUB_NAVY_HEX}"
title-text: ""
terminal-font: "Unicode Regular 16"

+ boot_menu {{
    left = 20%
    top = 46%
    width = 60%
    height = 34%
    item_font = "Unicode Regular 16"
    item_color = "#c9d1e0"
    selected_item_font = "Unicode Regular 16"
    selected_item_color = "#ffffff"
    item_height = 40
    item_padding = 10
    item_spacing = 6
    scrollbar = false
}}

+ progress_bar {{
    id = "__timeout__"
    left = 30%
    top = 84%
    width = 40%
    height = 6
    fg_color = "#3584e4"
    bg_color = "#1c2a45"
    border_color = "#1c2a45"
    show_text = false
    text = ""
}}

+ label {{
    left = 0
    top = 88%
    width = 100%
    align = "center"
    font = "Unicode Regular 16"
    color = "#8a94a6"
    text = "Starting KosherOS. Press Enter to start now."
}}
"""


def grub_background(src: Image.Image, size: tuple[int, int] = GRUB_SIZE) -> Image.Image:
    """Navy, with the lockup (mark and name) in the upper third, where the
    menu below it leaves room."""
    w, h = size
    canvas = Image.new("RGBA", size, GRUB_NAVY)
    mark = lockup(src, height=max(96, h // 8), gap=28, colour=GRUB_TEXT)
    if mark.width > w * 0.8:
        mark.thumbnail((int(w * 0.8), h), Image.LANCZOS)
    canvas.paste(mark, ((w - mark.width) // 2, int(h * 0.22) - mark.height // 2), mark)
    return canvas.convert("RGB")


def grub_theme_files(logo: Image.Image, font: bytes | None) -> dict[str, bytes]:
    """The theme directory's contents: theme.txt, background.png, and the
    font where the EFI copy needs to carry its own."""
    import io

    buffer = io.BytesIO()
    grub_background(logo).save(buffer, format="PNG", optimize=True)
    files = {"theme.txt": GRUB_THEME.encode(), "background.png": buffer.getvalue()}
    if font is not None:
        files["unicode.pf2"] = font
    return files


def read_grub_font() -> bytes | None:
    for candidate in GRUB_FONT_SOURCES:
        try:
            return Path(candidate).read_bytes()
        except OSError:
            continue
    return None


def background_xml(entries: list[tuple[tuple[int, int], Path]]) -> str:
    lines = ['<?xml version="1.0"?>',
             '<!DOCTYPE wallpapers SYSTEM "gnome-wp-list.dtd">',
             "<background>", "  <static>", "    <duration>8640000.0</duration>",
             "    <file>"]
    for (w, h), path in entries:
        lines.append(f'      <size width="{w}" height="{h}">{path}</size>')
    lines += ["    </file>", "  </static>", "</background>", ""]
    return "\n".join(lines)


def build(src_dir: Path, out: Path, version: str = "0") -> list[Path]:
    made: list[Path] = []

    def save(im: Image.Image, rel: str) -> None:
        path = out / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        im.save(path)
        made.append(path)

    def write(rel: str, data: bytes) -> None:
        path = out / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        made.append(path)

    logo = Image.open(src_dir / "logo.png").convert("RGBA")
    save(fitted(logo, 256), "usr/share/plymouth/themes/kosheros/logo.png")
    save(fitted(logo, 256), "usr/share/pixmaps/kosheros-logo.png")
    # The taskbar's Apps button: a house, not the mark (see home_icon).
    save(home_icon(256), "usr/share/pixmaps/kosheros-home.png")
    save(lockup(logo), "usr/share/pixmaps/kosheros-logo-login.png")
    # The admin app's icon is the KosherOS mark: it is the one app that IS
    # the product. Named by app id, which is how GNOME finds an app's icon.
    for size in (48, 128, 256, 512):
        save(fitted(logo, size),
             f"usr/share/icons/hicolor/{size}x{size}/apps/org.kosherlinux.Admin.png")

    wall = Image.open(src_dir / "wallpaper.png").convert("RGB")
    save(wall, "usr/share/backgrounds/kosheros/kosheros.png")
    entries = []
    for aspect, advertised in ASPECTS:
        rel = f"usr/share/backgrounds/kosheros/sizes/kosheros-{aspect[0]}x{aspect[1]}.png"
        save(top_left_crop(wall, aspect), rel)
        entries.append((advertised, Path("/") / rel))
    xml = out / "usr/share/backgrounds/kosheros/kosheros.xml"
    xml.write_text(background_xml(entries))
    made.append(xml)

    # The GRUB menu, twice: once where grub2-install picks it up for BIOS
    # machines, once where bootupd carries it to the EFI system partition.
    # The EFI copy carries the font too, because a UEFI-only install never
    # runs grub2-install and so has no fonts/ on its boot partition.
    theme = grub_theme_files(logo, None)
    for name, data in theme.items():
        write(f"{GRUB_THEME_DIR}/{name}", data)
    efi_dir = GRUB_EFI_COMPONENT.format(version=version)
    for name, data in grub_theme_files(logo, read_grub_font()).items():
        write(f"{efi_dir}/{name}", data)
    return made


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--src", type=Path, default=Path("/usr/share/kosher/branding"))
    p.add_argument("--out", type=Path, default=Path("/"),
                   help="root to write under (default: the filesystem root)")
    p.add_argument("--version", default=None,
                   help="product version (default: /usr/share/kosher/VERSION)")
    args = p.parse_args(argv)
    version = args.version
    if version is None:
        try:
            version = Path("/usr/share/kosher/VERSION").read_text().strip()
        except OSError:
            version = "0"
    for path in build(args.src, args.out, version):
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
