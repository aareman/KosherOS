#!/usr/bin/env python3
"""Every raster the image needs, from the two pieces of artwork.

Run at image build (the Containerfile copies branding/ to
/usr/share/kosher/branding and runs this); run locally with --out to see
what it makes. Pillow only — it is already in the image for the picture
filter.

From branding/logo.png:
  - the boot splash logo and the classic desktop's start button (256 px,
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


def lockup(src: Image.Image, height: int = 72, gap: int = 14) -> Image.Image:
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
              font=font, fill=WORDMARK_COLOUR)
    return canvas


def top_left_crop(src: Image.Image, aspect: tuple[int, int]) -> Image.Image:
    """The largest `aspect` crop of `src` that keeps its top-left corner."""
    aw, ah = aspect
    w, h = src.size
    if w * ah > h * aw:          # source is wider than the target: trim the right
        return src.crop((0, 0, h * aw // ah, h))
    return src.crop((0, 0, w, w * ah // aw))  # taller: trim the bottom


def background_xml(entries: list[tuple[tuple[int, int], Path]]) -> str:
    lines = ['<?xml version="1.0"?>',
             '<!DOCTYPE wallpapers SYSTEM "gnome-wp-list.dtd">',
             "<background>", "  <static>", "    <duration>8640000.0</duration>",
             "    <file>"]
    for (w, h), path in entries:
        lines.append(f'      <size width="{w}" height="{h}">{path}</size>')
    lines += ["    </file>", "  </static>", "</background>", ""]
    return "\n".join(lines)


def build(src_dir: Path, out: Path) -> list[Path]:
    made: list[Path] = []

    def save(im: Image.Image, rel: str) -> None:
        path = out / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        im.save(path)
        made.append(path)

    logo = Image.open(src_dir / "logo.png").convert("RGBA")
    save(fitted(logo, 256), "usr/share/plymouth/themes/kosheros/logo.png")
    save(fitted(logo, 256), "usr/share/pixmaps/kosheros-logo.png")
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
    return made


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--src", type=Path, default=Path("/usr/share/kosher/branding"))
    p.add_argument("--out", type=Path, default=Path("/"),
                   help="root to write under (default: the filesystem root)")
    args = p.parse_args(argv)
    for path in build(args.src, args.out):
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
