"""The gating matrix: who must prove what before a call runs.

These are the security decisions, so the tests are deliberately exhaustive
rather than representative — every method is asserted against every gate.
"""

import pytest

from kosherd import access
from kosherd.access import (
    ACTIONS,
    GUARDIAN_GATED,
    SETUP_CLOSED,
    SETUP_METHODS,
    UNKNOWN_METHOD,
    evaluate,
)

# A normal, fully set-up machine with a locked admin session.
LOCKED = dict(setup_complete=True, session_unlocked=False,
              guardian_enabled=False, guardian_proven=False)
UNLOCKED = {**LOCKED, "session_unlocked": True}
FRESH = {**LOCKED, "setup_complete": False}


def test_unknown_methods_are_refused():
    r = evaluate("DropAllRules", **LOCKED)
    assert not r.allowed and r.refusal == UNKNOWN_METHOD


def test_every_method_has_an_action():
    # A method missing from ACTIONS is refused outright, so a new D-Bus
    # method that forgets its entry fails closed rather than running
    # unguarded — but it also silently stops working, hence this check.
    for method in SETUP_METHODS | GUARDIAN_GATED | access.UID_AWARE:
        assert method in ACTIONS, f"{method} has no polkit action"


# -- reads are free, changes prompt -----------------------------------------

READS = ["GetPolicy", "Status", "IsEnabled", "ListCatalog", "ListInstalled",
         "ListInstalledDetails", "SearchApps", "CheckUpdate", "Lock",
         "VerifyGuardian", "PortalStatus", "SyncNow", "GetMyLayout"]
CHANGES = ["SetFilterMode", "SetWhitelist", "SetUrlRules", "CreateUser",
           "AdoptUser", "RemoveUser", "RemoveApp", "ApproveApp",
           "UnapproveApp", "SetUserApps", "SetUserCanInstall", "ApplyUpdate",
           "SetCaptiveMode", "SetGuardianPassword", "DisableGuardian",
           "SetGuestConfig", "Enrol", "Unenrol", "SetLayout", "SetCoverStyle", "SetAdBlock"]


@pytest.mark.parametrize("method", READS + CHANGES)
def test_locked_session_always_needs_polkit(method):
    assert evaluate(method, **LOCKED).polkit_action == ACTIONS[method]


@pytest.mark.parametrize("method", READS + CHANGES)
def test_unlocked_session_skips_polkit(method):
    # This is the "authenticate once" promise: after Unlock, nothing else
    # prompts for the rest of the session.
    assert evaluate(method, **UNLOCKED).polkit_action is None


def test_unlock_always_prompts_even_when_unlocked():
    # Otherwise a stale session could renew itself forever without anyone
    # proving they are still the admin.
    assert evaluate("Unlock", **UNLOCKED).polkit_action == ACTIONS["Unlock"]
    assert evaluate("Unlock", **LOCKED).polkit_action == ACTIONS["Unlock"]


def test_store_actions_are_open_to_ordinary_users():
    # The catalog is the allowlist, so using the store needs no admin rights;
    # the action itself is granted to any active local user by polkit.
    for method in ("ListCatalog", "ListInstalled", "InstallApp"):
        assert ACTIONS[method] == access.ACTION_USE_STORE


def test_reading_your_own_layout_is_open_to_ordinary_users():
    # The sign-in helper runs as whoever signs in, admin or not, and the
    # answer says nothing about how they are filtered. It must know WHO
    # asked, and it must not be a filter action a child could otherwise use.
    assert ACTIONS["GetMyLayout"] == access.ACTION_READ_OWN
    assert "GetMyLayout" in access.UID_AWARE
    assert access.ACTION_READ_OWN not in (access.ACTION_READ_CONFIG,
                                          access.ACTION_MANAGE_FILTER)


def test_choosing_a_layout_is_an_admin_action_without_the_guardian():
    # A preference, not a protection: no second password to change how the
    # desktop looks, but only an admin may change it for an account.
    assert ACTIONS["SetLayout"] == access.ACTION_MANAGE_USERS
    assert "SetLayout" not in GUARDIAN_GATED


def test_removing_an_app_is_admin_only():
    # Removal hits every user, unlike installing from the approved catalog.
    assert ACTIONS["RemoveApp"] == access.ACTION_INSTALL_APPS


# -- guardian dual control ---------------------------------------------------

@pytest.mark.parametrize("method", sorted(GUARDIAN_GATED))
def test_filter_changes_need_the_guardian_when_enabled(method):
    r = evaluate(method, **{**UNLOCKED, "guardian_enabled": True})
    assert r.needs_guardian


@pytest.mark.parametrize("method", sorted(GUARDIAN_GATED))
def test_guardian_is_proven_once_per_session(method):
    r = evaluate(method, **{**UNLOCKED, "guardian_enabled": True,
                            "guardian_proven": True})
    assert not r.needs_guardian


@pytest.mark.parametrize("method", sorted(GUARDIAN_GATED))
def test_no_guardian_prompt_when_the_feature_is_off(method):
    assert not evaluate(method, **UNLOCKED).needs_guardian


@pytest.mark.parametrize("method", [m for m in CHANGES if m not in GUARDIAN_GATED])
def test_non_filter_changes_never_need_the_guardian(method):
    r = evaluate(method, **{**UNLOCKED, "guardian_enabled": True})
    assert not r.needs_guardian


def test_the_guardian_gate_covers_every_filter_weakening_method():
    # Anything that can loosen filtering must be dual-controlled. If a new
    # filter method is added, it belongs in this list AND in GUARDIAN_GATED.
    expected = {"SetFilterMode", "SetWhitelist", "SetWhitelistBundles", "SetUrlRules",
                "SetBlockedCategories", "ApplyProfile", "SaveProfile",
                "DeleteProfile", "ApproveRequest", "AllowUrl",
                "EditList", "SetUserAdmin",
                "SetMediaLevel",
                "SetLanguageFilter", "SetYouTube", "SetGuestConfig",
                "DisableGuardian",
                "Enrol", "Unenrol", "SetAdBlock"}
    assert GUARDIAN_GATED == expected
    filter_actions = {m for m, a in ACTIONS.items()
                      if a == access.ACTION_MANAGE_FILTER}
    assert filter_actions <= GUARDIAN_GATED


# -- first-boot setup --------------------------------------------------------

def test_setup_methods_need_no_authorization_on_a_fresh_machine():
    # There is no admin yet who could authorize them.
    for method in SETUP_METHODS:
        r = evaluate(method, **FRESH)
        assert r.allowed and r.polkit_action is None and not r.needs_guardian


@pytest.mark.parametrize("method", ["CreateFirstAdmin", "FinishSetup"])
def test_setup_methods_close_permanently_once_complete(method):
    r = evaluate(method, **LOCKED)
    assert not r.allowed and r.refusal == SETUP_CLOSED


def test_reading_setup_state_stays_available_after_completion():
    # The wizard and the apps check this on every start.
    assert evaluate("IsComplete", **LOCKED).allowed


def test_an_interrupted_wizard_can_still_finish():
    # Completion is the stamp, not the existence of an admin: a wizard that
    # created the admin and then crashed must be able to set the guardian
    # and boot passwords on the next run.
    assert evaluate("FinishSetup", **FRESH).allowed


# -- the portal --------------------------------------------------------------

@pytest.mark.parametrize("method", ["Enrol", "Unenrol"])
def test_portal_enrolment_is_dual_controlled(method):
    # Enrolling hands filter control to a remote portal and unenrolling
    # takes it back, so both are filter changes.
    assert method in GUARDIAN_GATED
    assert ACTIONS[method] == access.ACTION_MANAGE_FILTER
    assert evaluate(method, **{**UNLOCKED, "guardian_enabled": True}).needs_guardian


def test_syncing_is_not_an_admin_action():
    # A sync only applies what the portal already signed; the Ed25519
    # signature and a strictly increasing revision authorise the content,
    # not the caller. Requiring the guardian here would break the timer.
    for method in ("SyncNow", "PortalStatus"):
        assert method not in GUARDIAN_GATED
        assert ACTIONS[method] == access.ACTION_READ_CONFIG
        assert not evaluate(method, **{**UNLOCKED,
                                       "guardian_enabled": True}).needs_guardian


def test_the_wizard_can_ask_whether_an_admin_exists_at_any_time():
    # Resume depends on this: a wizard that comes back after a failed finish
    # step asks first, and must not be told "setup is closed".
    assert evaluate("AdminExists", **FRESH).allowed
    assert evaluate("AdminExists", **LOCKED).allowed


def test_the_wizard_can_list_pre_existing_accounts_before_setup():
    assert evaluate("ExistingAccounts", **FRESH).allowed
    # ...but not forever: it is only the wizard's business.
    assert not evaluate("ExistingAccounts", **LOCKED).allowed
