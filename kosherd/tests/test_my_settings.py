"""What a user may learn about their own account.

Two things are being protected at once here, and they pull in opposite
directions. A filtered account should not be a mystery to the person using
it — that is the whole reason My Filter exists. But "read your own
settings" must not become a hole through which one account learns about
another, or through which a child learns when the machine is at its
weakest.
"""

import json

import pytest

from kosherd import access
from kosherd.access import ACTIONS
from kosherd.daemon import Daemon
from kosherd.policy import Policy, UserPolicy


def _settings(users, uid, **policy_kwargs):
    daemon = Daemon.__new__(Daemon)
    daemon.policy = Policy(revision=1, users=users, **policy_kwargs)
    return json.loads(daemon.impl_GetMySettings(_uid=uid).unpack()[0])


CHILD = UserPolicy(uid=1001, username="child", mode="filtered",
                   media_level="immodest", language_filter="substitute",
                   blocked_categories=["adult", "social"],
                   youtube={"restrict": "strict", "allowed_channels": ["a"]},
                   can_install_apps=False)
PARENT = UserPolicy(uid=1000, username="parent", mode="unfiltered",
                    admin=True)
LISTED = UserPolicy(uid=1002, username="listed", mode="whitelist",
                    whitelist=["chinuch.org", "aish.com"])


# ---- the gate ---------------------------------------------------------------

def test_reading_your_own_settings_is_open_to_ordinary_users():
    # A child must be able to open this without a password, so it cannot be
    # a read-config or manage-filter action. It must also be uid-aware, or
    # the daemon would not know whose settings to answer with.
    assert ACTIONS["GetMySettings"] == access.ACTION_READ_OWN
    assert "GetMySettings" in access.UID_AWARE


def test_the_filter_s_health_stays_admin_only():
    # "Picture checking has backed off right now" is a hint about when the
    # machine is weakest, so it must not ride along on the open action.
    assert ACTIONS["FilterStatus"] == access.ACTION_READ_CONFIG
    assert ACTIONS["GetMySettings"] != ACTIONS["FilterStatus"]


# ---- whose settings ---------------------------------------------------------

def test_the_answer_is_about_the_caller_and_nobody_else():
    settings = _settings([PARENT, CHILD, LISTED], uid=1001)
    assert settings["username"] == "child"
    assert settings["mode"] == "filtered"
    # Not one field mentions another account.
    assert "parent" not in json.dumps(settings)
    assert "listed" not in json.dumps(settings)


def test_an_unmanaged_account_is_told_so_rather_than_given_a_default():
    # Inventing a plausible-looking default here would be the worst
    # outcome: it would tell someone they are protected when they are not.
    settings = _settings([CHILD], uid=4242)
    assert settings["managed"] is False
    assert "mode" not in settings


def test_nothing_about_the_guardian_or_other_users_leaks():
    settings = _settings([PARENT, CHILD], uid=1001, guardian_enabled=True)
    blob = json.dumps(settings)
    for secret in ("guardian", "shadow", "password", "revision"):
        assert secret not in blob.lower()


# ---- what it says -----------------------------------------------------------

def test_a_filtered_account_sees_pictures_language_and_youtube():
    settings = _settings([CHILD], uid=1001)
    assert settings["media_level"] == "immodest"
    assert settings["language_filter"] == "substitute"
    assert settings["youtube"]["restrict"] == "strict"
    assert settings["can_install_apps"] is False


def test_categories_arrive_with_labels_a_person_can_read():
    settings = _settings([CHILD], uid=1001)
    labels = {c["label"] for c in settings["blocked_categories"]}
    assert "Social networks" in labels
    # The raw key is kept too, so the app never has to guess it back.
    assert {c["key"] for c in settings["blocked_categories"]} == {"adult",
                                                                  "social"}


def test_a_whitelist_account_can_see_the_whitelist():
    # Already discoverable through KosherOS Search by design, so listing it
    # reveals nothing new — and it is the question the app exists to answer.
    settings = _settings([LISTED], uid=1002)
    assert settings["whitelist"] == ["aish.com", "chinuch.org"]


@pytest.mark.parametrize("mode", ["none", "whitelist", "dnsfilter",
                                  "unfiltered"])
def test_picture_settings_are_hidden_where_they_mean_nothing(mode):
    # media_level only applies where pictures are actually judged. Showing
    # "show all images" to a whitelist account would be actively
    # misleading, because the whitelist is what governs there.
    user = UserPolicy(uid=1001, username="u", mode=mode, media_level="none")
    settings = _settings([user], uid=1001)
    assert "media_level" not in settings


def test_ad_blocking_is_reported_as_machine_wide():
    on = _settings([CHILD], uid=1001, adblock=True)
    off = _settings([CHILD], uid=1001, adblock=False)
    assert on["adblock"] is True
    assert off["adblock"] is False


def _app_constants():
    """Top-level literal assignments in My Filter.

    Read out of the source rather than imported, for the same reason
    test_admin_app_labels.py does it: the app needs GTK to import and the
    test environment has no reason to have it.
    """
    import ast
    from pathlib import Path

    app = Path(__file__).parents[2] / "myfilter-app/src/koshermyfilter/app.py"
    found = {}
    for node in ast.parse(app.read_text()).body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 \
                and isinstance(node.targets[0], ast.Name):
            try:
                found[node.targets[0].id] = ast.literal_eval(node.value)
            except ValueError:
                continue
    return found


def test_every_shipped_mode_has_words_for_the_person_being_filtered():
    # A mode the app cannot describe would show a child a raw string like
    # "dnsfilter". Adding a mode to policy.py and forgetting this table is
    # exactly the kind of omission nobody notices until a user sees it.
    from kosherd.policy import MODES as POLICY_MODES

    described = _app_constants()["MODES"]
    assert set(POLICY_MODES) == set(described), \
        "a filter mode exists that My Filter cannot explain"
    for mode, (title, description, icon) in described.items():
        assert title and description and icon, mode
        assert mode not in description.lower(), \
            f"{mode}: the explanation repeats the jargon instead of replacing it"


def test_the_time_words_cover_what_the_daemon_can_say():
    # The page fills these by key; a key the code asks for and the table
    # lacks is a KeyError on the child's screen.
    words = _app_constants()["TIME"]
    for key in ("title", "unlimited", "admin", "limit", "left", "used", "none_left",
                "today", "today_any", "today_never", "until", "warning"):
        assert words[key], key
    for jargon in ("uid", "logind", "pam", "kosherd", "daemon"):
        assert jargon not in " ".join(words.values()).lower(), jargon
    assert "{left}" in words["left"] and "{used}" in words["used"] and "{clock}" in words["until"]


def test_every_language_setting_has_words_too():
    from kosherd.language import MODES as LANGUAGE_MODES

    described = _app_constants()["LANGUAGE"]
    assert set(LANGUAGE_MODES) == set(described)


# ---- time --------------------------------------------------------------------

def test_your_own_time_limit_and_what_is_left_today_are_shown():
    from kosherd import timelimits

    limited = UserPolicy(uid=1001, username="child", mode="filtered",
                         time={"daily_minutes": 120,
                               "allowed": timelimits.SCHEDULE_PRESETS["not_late"]})
    settings = _settings([limited], uid=1001)
    t = settings["time"]
    assert t["limited"] is True and t["daily_minutes"] == 120 and t["admin"] is False
    assert len(t["allowed"]) == 7 and len(t["today"]) == 24
    assert t["today"] == timelimits.SCHEDULE_PRESETS["not_late"][0]
    assert t["used"] == 0 and t["left"] is not None


def test_an_unlimited_account_and_an_administrator_are_told_so():
    settings = _settings([CHILD], uid=1001)
    assert settings["time"]["limited"] is False and settings["time"]["left"] is None
    settings = _settings([PARENT], uid=1000)
    assert settings["time"]["admin"] is True and settings["time"]["limited"] is False
    assert settings["time"]["daily_minutes"] == 0
