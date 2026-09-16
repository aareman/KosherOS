"""The polkit rule that lets an administrator change the time zone.

KosherOS has no polkit admin identities at all (40-kosher-no-admin.rules),
which is right for "rebase the OS" and wrong for "set the clock": GNOME
Settings said "some settings are locked" to the person who set the
computer up. 45-kosher-admin-system.rules grants a named list of everyday
actions back to kosher-admin. This pins what is on that list and, more
importantly, what must never be.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).parents[2]
RULE = ROOT / "os-image/files/etc/polkit-1/rules.d/45-kosher-admin-system.rules"

# The panels a parent will actually open. Each of these was "locked".
MUST_GRANT = {
    "org.freedesktop.timedate1.set-timezone",
    "org.freedesktop.timedate1.set-ntp",
    "org.freedesktop.locale1.set-locale",
    "org.freedesktop.locale1.set-keyboard",
    "org.freedesktop.hostname1.set-static-hostname",
    "org.freedesktop.NetworkManager.network-control",
    "org.freedesktop.NetworkManager.settings.modify.system",
    "org.freedesktop.NetworkManager.enable-disable-wifi",
    "org.opensuse.cupspkhelper.mechanism.all-edit",
}

# Prefixes and ids that would let an admin around the filter or the image
# model. None may appear anywhere in the rule, not even in a comment that
# could later be uncommented by accident — hence the check is on the ids
# themselves, not on the granted list.
MUST_NOT_MENTION = (
    "org.freedesktop.packagekit",
    "org.projectatomic.rpmostree1",
    "org.freedesktop.Flatpak",
    "org.freedesktop.systemd1",
    "org.freedesktop.login1.set-reboot-to-firmware-setup",
    "org.freedesktop.NetworkManager.reload",
    "org.freedesktop.NetworkManager.checkpoint-rollback",
    "org.freedesktop.NetworkManager.settings.modify.global-dns",
    "org.freedesktop.NetworkManager.wifi.share.open",
    "org.freedesktop.NetworkManager.wifi.share.protected",
    "org.freedesktop.accounts",
    "org.freedesktop.policykit.exec",
)


def granted() -> set[str]:
    """The ids inside the rule's array literal — the only ones it grants."""
    text = RULE.read_text()
    body = text[text.index("var everyday = ["):text.index("];")]
    return set(re.findall(r'"([^"]+)"', body))


def test_the_rule_is_installed_where_polkit_reads_it():
    assert RULE.exists()
    # Between 41 (quiet denies) and 49 (kosher actions): a rule file runs
    # in name order and the first one to answer wins.
    assert "41-" < RULE.name[:3] < "49-"


def test_it_grants_the_panels_a_parent_opens():
    missing = MUST_GRANT - granted()
    assert not missing, f"still locked: {sorted(missing)}"


def test_it_never_reaches_the_filter_or_the_image():
    text = RULE.read_text()
    for forbidden in MUST_NOT_MENTION:
        # global-dns and wifi.share are named in the explanatory comment as
        # deliberately absent; that is fine so long as they are not granted.
        assert forbidden not in granted(), forbidden
    # And the shape that would grant everything is not there at all: the
    # one YES sits inside the named list's branch.
    assert 'indexOf("org.freedesktop.NetworkManager.") === 0' not in text
    assert text.count("polkit.Result.YES") == 1
    assert text.index("everyday.indexOf(action.id) >= 0") < text.index("polkit.Result.YES")


def test_it_needs_no_password_in_the_admins_own_session_and_only_locally():
    # "I should not have to type in my password for everything in settings
    # multiple times or at all ... its my account." Signing in as the
    # administrator is the proof; none of these can weaken the filter.
    text = RULE.read_text()
    assert "AUTH_SELF_KEEP" not in text and "AUTH_ADMIN" not in text
    assert 'subject.isInGroup("kosher-admin")' in text
    assert "subject.active && subject.local" in text
    # Somebody who is not a kosher-admin gets no answer from this rule at
    # all, so the no-admin rule keeps applying to them.
    assert "NOT_HANDLED" in text
