"""The GRUB menu wears the brand, on both firmware paths.

bootupd shows the menu for a second on every boot, so it is themed. The
theme must be reachable before the OS is up: on the EFI system partition
for UEFI (carried by bootupd as a component) and on the boot partition for
BIOS (carried by grub2-install as the default theme name). These pin both
copies, the snippet that loads them, and that the build registers the EFI
component so a theme cannot silently fail to reach the disk.
"""

import importlib.util
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
BRANDING = ROOT / "branding"
SNIPPET = ROOT / "os-image/files/usr/lib/bootupd/grub2-static/configs.d/09_kosheros_theme.cfg"

PIL = pytest.importorskip("PIL")
from PIL import Image  # noqa: E402

spec = importlib.util.spec_from_file_location("rasters_grub", BRANDING / "rasters.py")
rasters = importlib.util.module_from_spec(spec)
sys.modules["rasters_grub"] = rasters
spec.loader.exec_module(rasters)


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    out = tmp_path_factory.mktemp("root")
    rasters.build(BRANDING, out, version="0.1")
    return out


def test_the_theme_is_written_where_bios_and_uefi_each_find_it(built):
    bios = built / "usr/share/grub/themes/starfield"
    efi = built / "usr/lib/efi/kosheros-grub-theme/0.1/EFI/fedora/kosheros"
    for where in (bios, efi):
        assert (where / "theme.txt").is_file(), where
        assert (where / "background.png").is_file(), where
    assert (bios / "theme.txt").read_bytes() == (efi / "theme.txt").read_bytes()


def test_the_efi_copy_carries_its_own_font_when_the_build_host_has_one(built):
    # A UEFI-only install never runs grub2-install, so it has no fonts/ on
    # its boot partition; the font must travel with the theme. On a machine
    # without GRUB installed (a dev laptop) there is nothing to copy.
    efi = built / "usr/lib/efi/kosheros-grub-theme/0.1/EFI/fedora/kosheros"
    if Path("/usr/share/grub/unicode.pf2").exists():
        assert (efi / "unicode.pf2").stat().st_size > 100_000


def test_the_theme_only_names_files_that_exist_beside_it(built):
    theme_dir = built / "usr/share/grub/themes/starfield"
    text = (theme_dir / "theme.txt").read_text()
    for name in re.findall(r'"([^"]+\.png)"', text):
        assert (theme_dir / name).is_file(), name
    # Braces balance: GRUB's theme parser has no line numbers in its errors.
    assert text.count("{") == text.count("}") == 3
    assert 'id = "__timeout__"' in text, "the countdown is drawn, not just implied"
    assert 'title-text: ""' in text, "no GNU GRUB version banner"


def test_the_background_is_navy_with_the_lockup(built):
    im = Image.open(built / "usr/share/grub/themes/starfield/background.png").convert("RGB")
    assert im.size == (1920, 1080)
    assert im.getpixel((10, 1070)) == (13, 23, 41)          # the corner is navy
    upper = im.crop((0, 0, 1920, 540))
    assert len(set(upper.getdata())) > 500, "the mark and name are drawn in the upper half"
    lower = im.crop((0, 700, 1920, 1080))
    assert len(set(lower.getdata())) == 1, "the menu area is left clear"


def test_the_snippet_tries_uefi_then_bios_and_falls_through_quietly():
    text = SNIPPET.read_text()
    assert "${cmdpath}/kosheros/theme.txt" in text
    assert "${prefix}/themes/starfield/theme.txt" in text
    assert "if loadfont ${kosheros_font}; then" in text
    for module in ("gfxterm", "png", "all_video"):
        assert f"insmod {module}" in text
    assert "set theme=${kosheros_theme}" in text and "export theme" in text
    # It is sorted before the entries are listed and after greenboot.
    assert "08_greenboot" < SNIPPET.name < "10_blscfg"


def test_the_build_registers_the_efi_component_and_checks_it():
    containerfile = (ROOT / "os-image/Containerfile").read_text()
    assert "bootupctl backend generate-update-metadata" in containerfile
    assert "grep -q kosheros-grub-theme /usr/lib/bootupd/updates/EFI.json" in containerfile
    # The snippet ships in the image where bootupd assembles grub.cfg from.
    assert SNIPPET.is_file()


def test_the_version_in_the_efi_path_is_the_products(tmp_path):
    version = "0.6.0"  # any number: the path is what is under test
    rasters.build(BRANDING, tmp_path, version=version)
    assert (tmp_path / f"usr/lib/efi/kosheros-grub-theme/{version}/EFI/fedora/kosheros/theme.txt").is_file()
