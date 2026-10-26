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
         "VerifyGuardian", "PortalStatus", "SyncNow", "GetMyLayout", "GetTimeUsage",
         "SetMyLayout", "GetMyCompositor", "SetMyCompositor"]
CHANGES = ["SetFilterMode", "SetWhitelist", "SetUrlRules", "CreateUser",
           "AdoptUser", "RemoveUser", "RemoveApp", "InstallAppFor", "ApproveApp",
           "UnapproveApp", "SetUserApps", "SetUserCanInstall", "SetUserAppAccess",
           "SetUserBlockedAppKinds", "SetUserBlockedApps", "ApplyUpdate",
           "SetCaptiveMode", "SetGuardianPassword", "DisableGuardian",
           "SetGuestConfig", "Enrol", "Unenrol", "SetLayout", "SetCoverStyle", "SetAdBlock",
           "SetTimeLimits"]


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
    for method in ("ListCatalog", "ListStoreApps", "ListInstalled", "InstallApp"):
        assert ACTIONS[method] == access.ACTION_USE_STORE
    # What the store shows depends on who is asking, so the daemon must be
    # told the caller's uid.
    assert "ListStoreApps" in access.UID_AWARE


def test_installing_for_an_account_is_an_admins_action():
    # The Store's own action is answered only for the person at the
    # keyboard (a local, active session). Installing FOR an account is
    # how an administrator puts a tool on a child's account, and how a
    # shell or a test does it at all — an admin action, logged as one.
    assert ACTIONS["InstallAppFor"] == access.ACTION_INSTALL_APPS
    assert "InstallAppFor" in access.CHANGES
    assert "InstallAppFor" not in access.UID_AWARE  # the account is named


def test_reading_your_own_layout_is_open_to_ordinary_users():
    # The sign-in helper runs as whoever signs in, admin or not, and the
    # answer says nothing about how they are filtered. It must know WHO
    # asked, and it must not be a filter action a child could otherwise use.
    assert ACTIONS["GetMyLayout"] == access.ACTION_READ_OWN
    assert "GetMyLayout" in access.UID_AWARE
    assert access.ACTION_READ_OWN not in (access.ACTION_READ_CONFIG,
                                          access.ACTION_MANAGE_FILTER)


def test_choosing_your_own_layout_is_open_to_ordinary_users():
    # The Look app: a preference about yourself, no admin and no guardian.
    # It must know WHO asked, so nobody chooses for anybody else, and it
    # must be its own action, never a filter one.
    assert ACTIONS["SetMyLayout"] == access.ACTION_SET_OWN
    assert "SetMyLayout" in access.UID_AWARE
    assert "SetMyLayout" not in GUARDIAN_GATED
    assert access.ACTION_SET_OWN not in (access.ACTION_READ_CONFIG,
                                         access.ACTION_MANAGE_FILTER,
                                         access.ACTION_MANAGE_USERS)


def test_the_advanced_desktops_compositor_is_yours_like_the_layout():
    assert ACTIONS["GetMyCompositor"] == access.ACTION_READ_OWN
    assert ACTIONS["SetMyCompositor"] == access.ACTION_SET_OWN
    assert {"GetMyCompositor", "SetMyCompositor"} <= access.UID_AWARE
    assert "SetMyCompositor" not in GUARDIAN_GATED


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
                "Enrol", "Unenrol", "SetAdBlock", "SetTimeLimits",
                # Beyond the web: video calls and extra ports widen reach.
                "SetNetworkAccess",
                # Developer tools: the Nix daemon and the registries, directly.
                "SetDeveloperTools",
                # Another host a Nix build may download from (#199).
                "SetBuildSources",
                # The boot menu is the way round every other protection.
                "SetBootMenuPassword",
                # Not a filter setting, but the same kind of decision: it
                # puts the family computer on builds nobody has tried.
                "SetChannel",
                # A pull request's build, tried on purpose: the same decision.
                "TryImage",
                # Going back: the older version brings its older filter
                # with it (F11, #187).
                "Rollback",
                # Opening an account to the whole store, or lifting an app
                # block, widens what it can reach.
                "SetUserAppAccess", "SetUserBlockedAppKinds", "SetUserBlockedApps",
                # Syncthing for one account (#213): files from paired devices
                # are not filtered, and its ports go out directly; letting
                # the home network reach it opens the input side.
                "SetUserAppApproval", "SetSyncthingLan",
                # Joining a tailnet puts the computer on a network with
                # other people's machines, reachable by every account.
                "TailscaleSignIn"}
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


# -- Tailscale (#30) ---------------------------------------------------------

def test_reading_the_tailnet_status_is_a_read():
    assert evaluate("TailscaleStatus", **LOCKED).polkit_action == access.ACTION_READ_CONFIG
    assert not evaluate("TailscaleStatus",
                        **{**UNLOCKED, "guardian_enabled": True}).needs_guardian


def test_signing_in_and_out_are_network_actions_for_an_administrator():
    for method in ("TailscaleSignIn", "TailscaleSignOut"):
        assert ACTIONS[method] == access.ACTION_MANAGE_NETWORK
        assert evaluate(method, **LOCKED).polkit_action == access.ACTION_MANAGE_NETWORK
        # Both are written to the activity log with the admin who did it.
        assert method in access.CHANGES


def test_joining_a_tailnet_takes_the_guardian_and_leaving_does_not():
    guarded = {**UNLOCKED, "guardian_enabled": True}
    assert evaluate("TailscaleSignIn", **guarded).needs_guardian
    assert not evaluate("TailscaleSignIn",
                        **{**guarded, "guardian_proven": True}).needs_guardian
    assert not evaluate("TailscaleSignOut", **guarded).needs_guardian


def test_the_tailnet_methods_are_on_the_bus_and_run_off_the_main_loop():
    from kosherd import daemon

    for method in ("TailscaleStatus", "TailscaleSignIn", "TailscaleSignOut"):
        assert f'<method name="{method}"' in daemon.INTROSPECTION_XML
        assert method in daemon.IN_BACKGROUND
        assert hasattr(daemon.Daemon, f"impl_{method}")


# -- one prompt per sitting (#181, #182) ----------------------------------------

def test_a_client_opens_the_session_before_anything_that_would_prompt():
    # polkit keeps an answered prompt per action, so `kosherctl set-mode`
    # on a child's account asked twice: read-config for the guardian
    # check, then manage-filter for the change. A client that opens the
    # session first asks once, for Unlock, which needs the same
    # administrator's password as either of them.
    assert access.needs_admin_session("IsEnabled")
    assert access.needs_admin_session("SetFilterMode")
    assert access.needs_admin_session("Rollback")
    assert ACTIONS["Unlock"] not in access.FREE_ACTIONS


@pytest.mark.parametrize("method", ["Unlock", "Lock", "IsComplete", "AdminExists",
                                    "GetMySettings", "GetMyLayout", "SetMyLayout",
                                    "ListStoreApps", "ListMyInstalledDetails",
                                    "InstallApp", "NoSuchMethod"])
def test_free_and_setup_calls_never_open_a_session(method):
    # Opening one would put a password prompt in front of something every
    # account may do without one, or in front of the wizard.
    assert not access.needs_admin_session(method)


def test_the_free_actions_are_the_ones_the_rule_answers_yes_to():
    import pathlib

    rule = (pathlib.Path(__file__).parents[2]
            / "os-image/files/etc/polkit-1/rules.d/49-kosher-admin.rules").read_text()
    for action in access.FREE_ACTIONS:
        assert f'"{action}"' in rule
    for action in set(ACTIONS.values()) - access.FREE_ACTIONS:
        assert f'"{action}"' not in rule, f"{action} must stay behind the password"


def test_the_store_lists_what_is_installed_without_a_password():
    # #182: the Store asked a child's account for an administrator's
    # password because it read ListInstalledDetails, which names who
    # installed what and lists every account's own apps. That stays an
    # admin read; the Store reads the caller's own view instead.
    assert ACTIONS["ListInstalledDetails"] == access.ACTION_READ_CONFIG
    assert ACTIONS["ListMyInstalledDetails"] == access.ACTION_USE_STORE
    assert "ListMyInstalledDetails" in access.UID_AWARE
    assert ACTIONS["GetPolicy"] == access.ACTION_READ_CONFIG
