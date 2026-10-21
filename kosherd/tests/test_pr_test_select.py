"""What `just pr-test` runs for a change, by the paths it touches (#173)."""

import sys
from pathlib import Path

ROOT = Path(__file__).parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from pr_test_select import FILTER_SUITES, SUITES, select  # noqa: E402


def test_docs_alone_run_nothing():
    sel = select(["docs/testing.md", "README.md", "kosherd/tests/test_nft.py"])
    assert sel.empty
    assert all("nothing to run" in r for r in sel.reasons)


def test_a_desktop_app_runs_the_signed_in_session_only():
    sel = select(["look-app/src/kosherlook/app.py"])
    assert sel.desktop == {"session"} and not sel.suites and not sel.boot


def test_a_check_on_the_signed_in_desktop_runs_the_session():
    sel = select(["scripts/vm-tests/desktop/keyd.sh", "scripts/vm-tests/desktop/keys.py"])
    assert sel.desktop == {"session"} and not sel.suites and not sel.boot


def test_the_filter_runs_the_filtering_suites_and_the_block_page():
    sel = select(["kosherd/src/kosherd/nft.py", "mitm/kosher_filter.py"])
    assert sel.suites == set(FILTER_SUITES)
    assert sel.desktop == {"filter"} and not sel.boot


def test_what_changes_power_on_to_setup_is_a_fresh_install():
    for path in ("os-image/files/usr/lib/bootupd/grub2-static/configs.d/09_kosheros_theme.cfg",
                 "os-image/files/usr/lib/systemd/system/kosher-firstboot.service",
                 "setup-app/src/koshersetup/app.py", "os-image/dev-config.toml"):
        sel = select([path])
        assert sel.boot, path
        assert sel.suites == set(SUITES)


def test_the_containerfile_is_a_fresh_install_only_where_it_touches_booting():
    plain = select(["os-image/Containerfile"], "+RUN dnf install -y htop\n")
    assert not plain.boot and plain.suites == set(SUITES)
    kargs = select(["os-image/Containerfile"], '+    && printf \'kargs = ["quiet"]\'\n')
    assert kargs.boot


def test_a_suite_runs_itself():
    sel = select(["scripts/vm-tests/40-guest.sh"])
    assert sel.suites == {"40-guest"} and not sel.desktop


def test_an_unknown_path_runs_everything_and_flags_override():
    assert select(["something/new.txt"]).suites == set(SUITES)
    assert select(["docs/x.md"], everything=True).suites == set(SUITES)
    full = select(["docs/x.md"], full=True)
    assert full.boot and full.suites == set(SUITES) and full.desktop == {"session", "filter"}
