"""The boot menu password: set by default, readable, settable later (#129).

Editing the boot menu is the way round every other protection — a kernel
argument typed at the keyboard starts the computer without the filter. The
wizard has been able to set a GRUB password since the start, but as an
opt-in switch nobody knew to use. Now it is on by default and takes the
administrator's own password, the text setup does the same, and an
administrator can set or change it later from the Protection page.
"""

import pytest

from kosherd import access, daemon as daemon_mod
from kosherd.daemon import Daemon, PolicyError


@pytest.fixture
def d(monkeypatch, tmp_path):
    inst = Daemon.__new__(Daemon)
    monkeypatch.setattr(daemon_mod, "GRUB_USER_CFG", tmp_path / "user.cfg")
    return inst


def test_the_switch_is_a_filter_change_whose_argument_is_never_logged():
    assert access.ACTIONS["SetBootMenuPassword"] == access.ACTION_MANAGE_FILTER
    assert "SetBootMenuPassword" in access.GUARDIAN_GATED
    assert "SetBootMenuPassword" in access.CHANGES
    assert "SetBootMenuPassword" in access.NO_ARGS_LOGGED
    assert access.ACTIONS["BootMenuProtected"] == access.ACTION_READ_CONFIG


def test_protected_means_a_real_hash_in_user_cfg(d, tmp_path):
    assert d.impl_BootMenuProtected().unpack() == (False,)
    (tmp_path / "user.cfg").write_text("GRUB2_PASSWORD=grub.pbkdf2.sha512.10000.AB.CD\n")
    assert d.impl_BootMenuProtected().unpack() == (True,)
    # the conditional line Fedora's own config carries is not a password
    (tmp_path / "user.cfg").write_text("set superusers=root\npassword_pbkdf2 root ${GRUB2_PASSWORD}\n")
    assert d.impl_BootMenuProtected().unpack() == (False,)


def test_setting_hashes_and_writes_through_the_existing_path(d, monkeypatch):
    seen = []
    monkeypatch.setattr(Daemon, "_set_grub_password", staticmethod(lambda pw: seen.append(pw)))
    d.impl_SetBootMenuPassword("Correct-Horse-42", "")
    assert seen == ["Correct-Horse-42"]


def test_a_short_password_is_refused_before_anything_is_written(d, monkeypatch):
    monkeypatch.setattr(Daemon, "_set_grub_password",
                        staticmethod(lambda pw: pytest.fail("wrote a short password")))
    with pytest.raises(PolicyError):
        d.impl_SetBootMenuPassword("short", "")
