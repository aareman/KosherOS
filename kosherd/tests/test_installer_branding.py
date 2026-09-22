"""The installer wears the brand: product.img, built here, over Anaconda.

Anaconda unpacks images/product.img from the install media over its own
root before it starts, so a small archive can rename the product, point
the UI at our stylesheet and supply the logo without rebuilding anything.
These tests build that archive and read it back with the system's cpio,
which is what the initramfs does.
"""

import configparser
import gzip
import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
SCRIPT = ROOT / "scripts/brand-iso.py"

pytest.importorskip("PIL")

spec = importlib.util.spec_from_file_location("brand_iso", SCRIPT)
brand = importlib.util.module_from_spec(spec)
sys.modules["brand_iso"] = brand
spec.loader.exec_module(brand)


def _listing(img: Path) -> list[str]:
    if shutil.which("cpio") is None:
        pytest.skip("cpio not installed")
    result = subprocess.run(["cpio", "-it", "--quiet"], input=gzip.decompress(img.read_bytes()),
                            capture_output=True, check=True)
    return result.stdout.decode().split()


def _extract(img: Path, into: Path) -> None:
    subprocess.run(["cpio", "-id", "--quiet"], input=gzip.decompress(img.read_bytes()),
                   cwd=into, check=True, capture_output=True)


@pytest.fixture(scope="module")
def img(tmp_path_factory):
    dest = tmp_path_factory.mktemp("iso") / "product.img"
    return brand.product_img(dest)


def test_the_archive_is_a_cpio_the_initramfs_can_unpack(img):
    names = _listing(img)
    assert ".buildstamp" in names
    assert "etc/anaconda/conf.d/90-kosheros.conf" in names
    assert "usr/share/anaconda/pixmaps/kosheros/kosheros.css" in names
    assert "usr/share/anaconda/pixmaps/kosheros/sidebar-logo.png" in names
    assert "TRAILER!!!" not in names


def test_the_buildstamp_names_the_product(img, tmp_path):
    _extract(img, tmp_path)
    stamp = configparser.ConfigParser()
    stamp.read(tmp_path / ".buildstamp")
    assert stamp["Main"]["Product"] == "KosherOS"
    assert stamp["Main"]["Version"] == brand.VERSION
    assert stamp["Main"]["IsFinal"] == "false"
    assert stamp["Main"]["BugURL"].startswith("https://github.com/aareman/KosherOS")


def test_the_dropin_points_anaconda_at_our_stylesheet(img, tmp_path):
    _extract(img, tmp_path)
    conf = configparser.ConfigParser()
    conf.read(tmp_path / "etc/anaconda/conf.d/90-kosheros.conf")
    sheet = conf["User Interface"]["custom_stylesheet"]
    assert sheet == "/usr/share/anaconda/pixmaps/kosheros/kosheros.css"
    assert (tmp_path / sheet.lstrip("/")).is_file()


def test_the_installer_is_one_question_and_a_button(img, tmp_path):
    # The hub used to show seven steps in any order. Everything the ISO's
    # kickstart already answers is hidden, so only the disk is asked.
    _extract(img, tmp_path)
    conf = configparser.ConfigParser()
    conf.read(tmp_path / "etc/anaconda/conf.d/90-kosheros.conf")
    hidden = set(conf["User Interface"]["hidden_spokes"].split())
    assert hidden == {"KeyboardSpoke", "LangsupportSpoke", "DatetimeSpoke",
                      "NetworkSpoke", "SourceSpoke", "SoftwareSelectionSpoke"}
    assert "StorageSpoke" not in hidden, "the disk is the one question that must be asked"
    # Each hidden spoke is one the kickstart or the module list has answered.
    kickstart = (ROOT / "os-image/iso-config.toml").read_text()
    for needed in ("keyboard", "lang ", "timezone"):
        assert needed in kickstart
    assert "org.fedoraproject.Anaconda.Modules.Users" in kickstart


def test_the_stylesheet_only_references_files_in_the_archive(img, tmp_path):
    import re

    _extract(img, tmp_path)
    css = (tmp_path / "usr/share/anaconda/pixmaps/kosheros/kosheros.css").read_text()
    urls = re.findall(r"url\('([^']+)'\)", css)
    assert urls, "the sidebar logo is drawn from the stylesheet"
    for url in urls:
        assert (tmp_path / url.lstrip("/")).is_file(), url
    # Fedora's own stylesheet path is overwritten with the same file, so
    # the branding holds even if the drop-in were not read.
    assert (tmp_path / "usr/share/anaconda/pixmaps/fedora.css").read_text() == css


def test_the_sidebar_logo_is_the_real_mark_fitted_square(img, tmp_path):
    from PIL import Image

    _extract(img, tmp_path)
    logo = Image.open(tmp_path / "usr/share/anaconda/pixmaps/kosheros/sidebar-logo.png")
    assert logo.size == (brand.LOGO_SIZE, brand.LOGO_SIZE)
    assert logo.mode == "RGBA"


def test_files_in_the_archive_belong_to_root(img):
    # An installer root file owned by uid 1000 would be wrong, and the
    # header carries uid/gid explicitly.
    raw = gzip.decompress(img.read_bytes())
    for offset in range(0, len(raw) - 110):
        if raw[offset:offset + 6] == b"070701":
            uid = raw[offset + 22:offset + 30]
            gid = raw[offset + 30:offset + 38]
            assert uid == b"00000000" and gid == b"00000000"
            break
    else:
        pytest.fail("no newc header found")


def test_the_whole_archive_is_small(img):
    # It rides on every ISO and is unpacked into RAM; a logo and two text
    # files should stay well under a megabyte.
    assert img.stat().st_size < 400_000


@pytest.mark.skipif(shutil.which("xorriso") is None, reason="xorriso not installed")
def test_injecting_into_an_iso_keeps_it_bootable_shaped(img, tmp_path):
    # A tiny ISO stands in for the installer: after injection it must carry
    # images/product.img and still be a readable ISO 9660 image.
    tree = tmp_path / "tree" / "images"
    tree.mkdir(parents=True)
    (tree / "install.img").write_bytes(b"not really a squashfs")
    iso = tmp_path / "install.iso"
    subprocess.run(["xorriso", "-as", "mkisofs", "-o", str(iso), "-J", "-R",
                    str(tmp_path / "tree")], check=True, capture_output=True)
    brand.inject(iso, img)
    listing = subprocess.run(["xorriso", "-indev", str(iso), "-ls", "/images", "-end"],
                             check=True, capture_output=True, text=True).stdout
    assert "product.img" in listing and "install.img" in listing


def test_the_command_line_builds_only_the_image_when_asked(tmp_path):
    dest = tmp_path / "out" / "product.img"
    assert brand.main(["--product-img", str(dest)]) == 0
    assert dest.is_file()


def test_the_release_name_is_version_and_arch_only():
    # No date: the version is distinct per commit, and a date would make
    # one release look like two files.
    assert brand.release_name("0.1.0-pre.37", arch="x86_64") == "KosherOS-0.1.0-pre.37-x86_64.iso"


def test_renaming_leaves_install_iso_pointing_at_the_release(tmp_path):
    iso = tmp_path / "install.iso"
    iso.write_bytes(b"iso")
    named = brand.rename(iso, "0.1")
    assert named.name.startswith("KosherOS-0.1-") and named.name.endswith(".iso")
    assert named.name.count("-") == 2, "version and arch only, no date"
    assert iso.is_symlink() and iso.resolve() == named.resolve()
    assert iso.read_bytes() == b"iso"
    # A second build of the same version replaces the release file cleanly.
    iso.unlink()
    iso.write_bytes(b"newer")
    again = brand.rename(iso, "0.1")
    assert again == named and iso.read_bytes() == b"newer"


def test_one_version_number_for_the_whole_product():
    # The tags are the only place the number lives (scripts/version.py); the
    # build passes it in, the Containerfile writes it into the image and
    # os-release, and the ISO name and .buildstamp read the same value.
    shown = subprocess.run([sys.executable, str(ROOT / "scripts/version.py"), "show"],
                           check=True, capture_output=True, text=True).stdout.strip()
    assert shown and shown == brand.VERSION
    containerfile = (ROOT / "os-image/Containerfile").read_text()
    assert '"$KOSHER_VERSION" > /usr/share/kosher/VERSION' in containerfile
    assert "COPY VERSION" not in containerfile, "there is no VERSION file to copy"
    assert "KosherOS 0.1" not in containerfile, "the version must not be hard-coded"


def test_without_xorriso_or_nix_the_message_says_what_to_do(monkeypatch):
    monkeypatch.setattr(brand.shutil, "which", lambda name: None)
    with pytest.raises(SystemExit) as raised:
        brand._xorriso_runner()
    assert "devenv shell" in str(raised.value) and "just brand-iso" in str(raised.value)


def test_a_stale_shell_falls_back_to_nix(monkeypatch):
    monkeypatch.setattr(brand.shutil, "which",
                        lambda name: "/nix/bin/nix" if name == "nix" else None)
    calls = []
    monkeypatch.setattr(brand.subprocess, "run",
                        lambda args, **kw: calls.append(args) or None)
    brand._xorriso_runner()(["-version"])
    assert calls[0][:5] == ["nix", "shell", "nixpkgs#xorriso", "-c", "xorriso"]



def test_the_boot_menu_is_hidden_but_reachable():
    """Boot straight in; the menu stays one keypress away.

    The user: "the boot choice options screen should be hidden by default
    and just go straight to boot the os". Hidden with a one-second silent
    countdown, not timeout 0: with no countdown at all there is no moment
    in which Shift or Esc can open the menu, and a machine that will not
    start becomes a machine that cannot be reached.
    """
    cfg = (ROOT / "os-image/files/usr/lib/bootupd/grub2-static/configs.d"
           / "09_kosheros_quiet.cfg").read_text()
    assert "set timeout_style=hidden" in cfg
    assert "set timeout=1" in cfg
    assert "set timeout=0" not in cfg
    containerfile = (ROOT / "os-image/Containerfile").read_text()
    assert "GRUB_TIMEOUT_STYLE=hidden" in containerfile


def test_the_person_model_is_fetched_by_hash():
    from kosherd import persons

    containerfile = (ROOT / "os-image/Containerfile").read_text()
    assert persons.MODEL_URL in containerfile
    assert persons.MODEL_SHA256 in containerfile
    assert str(persons.MODEL_PATH) in containerfile or "/usr/share/kosher/models/yolox_nano.onnx" in containerfile


def test_the_boot_splash_speaks_in_sentences_not_unit_names():
    """'Can we make the text shown of what is happening more user friendly
    than just plymouth etc.' systemd's status lines are off the splash;
    our units announce themselves in words through plymouth's message
    channel, and the theme opens on one of ours."""
    containerfile = (ROOT / "os-image/Containerfile").read_text()
    assert "systemd.show_status" not in containerfile.split("kargs = [")[1].split("]")[0]
    script = (ROOT / "os-image/files/usr/share/plymouth/themes/kosheros/kosheros.script").read_text()
    assert 'show_status("Starting KosherOS…");' in script
    units = ROOT / "os-image/files/usr/lib/systemd/system"
    said = {}
    for conf in units.glob("*.service.d/10-kosheros-splash.conf"):
        text = conf.read_text()
        assert "ExecStartPre=-/usr/bin/plymouth display-message --text=" in text, conf
        said[conf.parent.name] = text.split('--text="')[1].split('"')[0]
    assert set(said) >= {"kosherd.service.d", "greenboot-healthcheck.service.d",
                         "kosher-firstboot.service.d"}
    for unit, words in said.items():
        assert words.endswith("…") and "service" not in words.lower(), (unit, words)
        assert words[0].isupper(), words
