"""Who may call what — the daemon's gating decisions, as pure logic.

Every kosherd method passes three gates before it runs: is this a
first-boot setup call (allowed only until setup completes), does the caller
need a polkit prompt (only when their admin session is not already open),
and does the call additionally need the guardian password.

That logic used to live inline in the D-Bus dispatcher, where it could only
be exercised by driving a real bus in a VM. It lives here so the whole
matrix is unit-testable: these decisions are the security model, and a
mistake in them is not visible from looking at a running system.
"""

from __future__ import annotations

from dataclasses import dataclass

# polkit action ids, declared in os-image/.../org.kosherlinux.policy.
ACTION_READ_CONFIG = "org.kosherlinux.read-config"
# Granted to every active local user: the catalog is the allowlist, so
# installing from it is safe for supervised users too.
ACTION_USE_STORE = "org.kosherlinux.use-store"
# Granted to every active local user: reading a setting about YOURSELF that
# says nothing about how you are filtered (today: the desktop layout, which
# the sign-in helper applies).
ACTION_READ_OWN = "org.kosherlinux.read-own-settings"
ACTION_MANAGE_USERS = "org.kosherlinux.manage-users"
ACTION_MANAGE_FILTER = "org.kosherlinux.manage-filter"
ACTION_INSTALL_APPS = "org.kosherlinux.install-apps"
ACTION_MANAGE_NETWORK = "org.kosherlinux.manage-network"
ACTION_APPLY_UPDATES = "org.kosherlinux.apply-updates"
ACTION_MANAGE_GUARDIAN = "org.kosherlinux.manage-guardian"

# method -> polkit action.
ACTIONS = {
    # First-boot setup. Authorized ONLY while the machine has no admin yet;
    # afterwards these are refused outright, so this is not a standing
    # privilege-escalation path.
    "IsComplete": ACTION_READ_CONFIG,
    "AdminExists": ACTION_READ_CONFIG,
    "ExistingAccounts": ACTION_READ_CONFIG,
    "CreateFirstAdmin": ACTION_MANAGE_USERS,
    "FinishSetup": ACTION_MANAGE_USERS,
    # Getting the new machine online from the wizard. First-boot only like
    # the rest of this block; afterwards Wi-Fi is GNOME Settings' job, which
    # the everyday polkit rule lets an administrator use.
    "NetworkStatus": ACTION_READ_CONFIG,
    "ListWifi": ACTION_READ_CONFIG,
    "ConnectWifi": ACTION_MANAGE_NETWORK,
    # Unlock is THE prompt: one polkit check opens a sliding session, after
    # which the admin works without re-authenticating (see session.py).
    "Unlock": ACTION_MANAGE_USERS,
    "Lock": ACTION_READ_CONFIG,
    "Status": ACTION_READ_CONFIG,
    "VerifyGuardian": ACTION_READ_CONFIG,
    "GetPolicy": ACTION_READ_CONFIG,
    "SetFilterMode": ACTION_MANAGE_FILTER,
    "SetWhitelist": ACTION_MANAGE_FILTER,
    "ListWhitelistBundles": ACTION_READ_CONFIG,
    # Switching a ready-made approved-site list on widens what a whitelist
    # account can reach, so it is a filter change like editing the list.
    "SetWhitelistBundles": ACTION_MANAGE_FILTER,
    # Enrolling hands filter control to a portal and unenrolling takes it
    # back, so both are filter changes. Syncing only applies what the portal
    # already signed — the Ed25519 signature and a strictly increasing
    # revision authorise that content, not the caller.
    "Enrol": ACTION_MANAGE_FILTER,
    "Unenrol": ACTION_MANAGE_FILTER,
    "SyncNow": ACTION_READ_CONFIG,
    "PortalStatus": ACTION_READ_CONFIG,
    "SetUrlRules": ACTION_MANAGE_FILTER,
    "SetBlockedCategories": ACTION_MANAGE_FILTER,
    "ApplyProfile": ACTION_MANAGE_FILTER,
    "SaveProfile": ACTION_MANAGE_FILTER,
    "DeleteProfile": ACTION_MANAGE_FILTER,
    "SetUserAdmin": ACTION_MANAGE_FILTER,
    # The desktop layout is a preference, not a protection, so it is an
    # account-management action with no guardian gate: the filter is
    # per-uid at the network layer whatever draws the windows.
    "SetLayout": ACTION_MANAGE_USERS,
    "GetMyLayout": ACTION_READ_OWN,
    # Your own filter settings, read-only, no password: what applies to
    # THIS account and nothing about any other. A filtered account should
    # not be a mystery to the person using it — someone who can see the
    # rules is likelier to accept them than someone who only ever meets a
    # blocked page. Reading this reveals nothing that helps evade it: the
    # mode is already obvious from using the machine, and the whitelist is
    # deliberately discoverable through search. Deliberately NOT included
    # is anything about the filter's current health (FilterStatus stays
    # ACTION_READ_CONFIG), because "picture checking has backed off right
    # now" is a hint about when the machine is weakest.
    "GetMySettings": ACTION_READ_OWN,
    # How a covered picture looks. Cosmetic — the media level decides what
    # is judged and cannot be loosened here — so an account-management
    # action rather than a filter one, and no guardian gate.
    "SetCoverStyle": ACTION_MANAGE_USERS,
    # Machine-wide ad and tracker blocking at the resolvers. Turning it off
    # weakens what reaches the screen, so it is guardian-gated.
    "SetAdBlock": ACTION_MANAGE_FILTER,
    # How long and when an account may be signed in. Loosening a limit
    # weakens the protection like any other filter change, so it takes
    # the same road: a filter action, guardian-gated.
    "SetTimeLimits": ACTION_MANAGE_FILTER,
    "GetTimeUsage": ACTION_READ_CONFIG,
    "ApproveRequest": ACTION_MANAGE_FILTER,
    "DismissRequest": ACTION_MANAGE_USERS,
    "ListRequests": ACTION_READ_CONFIG,
    # Allowing a blocked page from the activity view is the same grant as
    # approving a request for it.
    "AllowUrl": ACTION_MANAGE_FILTER,
    "ListActivity": ACTION_READ_CONFIG,
    "ActivitySummary": ACTION_READ_CONFIG,
    "SetMediaLevel": ACTION_MANAGE_FILTER,
    "SetLanguageFilter": ACTION_MANAGE_FILTER,
    "SetYouTube": ACTION_MANAGE_FILTER,
    "ListProfiles": ACTION_READ_CONFIG,
    "FilterStatus": ACTION_READ_CONFIG,
    "GetListEdits": ACTION_READ_CONFIG,
    "EditList": ACTION_MANAGE_FILTER,
    "ListCategories": ACTION_READ_CONFIG,
    "SetGuestConfig": ACTION_MANAGE_FILTER,
    "CreateUser": ACTION_MANAGE_USERS,
    "AdoptUser": ACTION_MANAGE_USERS,
    "RemoveUser": ACTION_MANAGE_USERS,
    # The app store is for everyone: browsing and installing from the
    # pre-approved catalog need no admin password (kosherd still checks the
    # per-user can_install_apps flag). Removing affects every user, so it
    # stays an admin action.
    "ListCatalog": ACTION_USE_STORE,
    "ListInstalled": ACTION_USE_STORE,
    "InstallApp": ACTION_USE_STORE,
    "ListInstalledDetails": ACTION_READ_CONFIG,
    "RemoveApp": ACTION_INSTALL_APPS,
    "SetUserApps": ACTION_INSTALL_APPS,
    "SetUserCanInstall": ACTION_INSTALL_APPS,
    "SearchApps": ACTION_READ_CONFIG,
    "ApproveApp": ACTION_INSTALL_APPS,
    "UnapproveApp": ACTION_INSTALL_APPS,
    "CheckUpdate": ACTION_READ_CONFIG,
    "ApplyUpdate": ACTION_APPLY_UPDATES,
    "DeploymentStatus": ACTION_READ_CONFIG,
    # Going back to the previously booted image. Not guardian-gated: the
    # image is one this machine already ran, and greenboot must be able to
    # do the same thing with no password when a new image fails to bring
    # the filter up. Gating it would mainly risk a broken machine nobody
    # present can repair. See impl_Rollback.
    "Rollback": ACTION_APPLY_UPDATES,
    # The Restart button after an update. Same action as applying it: a
    # restart is the second half of the same job.
    "Reboot": ACTION_APPLY_UPDATES,
    "SetCaptiveMode": ACTION_MANAGE_NETWORK,
    "IsEnabled": ACTION_READ_CONFIG,
    "SetGuardianPassword": ACTION_MANAGE_GUARDIAN,
    "DisableGuardian": ACTION_MANAGE_GUARDIAN,
}

# Methods that can weaken the filter: guardian password required when enabled.
GUARDIAN_GATED = frozenset({
    "SetFilterMode", "SetWhitelist", "SetWhitelistBundles", "SetUrlRules", "SetBlockedCategories", "ApplyProfile", "SaveProfile", "DeleteProfile", "ApproveRequest", "AllowUrl", "EditList",
    "SetUserAdmin",
    "SetMediaLevel",
    "SetLanguageFilter", "SetYouTube",
    "SetGuestConfig", "DisableGuardian", "Enrol", "Unenrol",
    "SetAdBlock", "SetTimeLimits",
})

# First-boot only; closed forever once setup is stamped complete.
SETUP_METHODS = frozenset({"IsComplete", "AdminExists", "ExistingAccounts",
                           "CreateFirstAdmin", "FinishSetup",
                           "NetworkStatus", "ListWifi", "ConnectWifi"})
# Readable forever: the wizard and the apps ask these on every start.
SETUP_READS = frozenset({"IsComplete", "AdminExists"})

# Methods that need to know which uid called them (session management).
UID_AWARE = frozenset({"Unlock", "Lock", "Status", "VerifyGuardian", "InstallApp",
                       "GetMyLayout", "GetMySettings"})

# Calls that change how the machine is set up, written to the activity log
# with the admin who made them so "who changed this?" has an answer. Reads,
# session calls and the store's own installs are not changes to the family's
# settings and are not logged.
CHANGES = frozenset({
    "SetFilterMode", "SetWhitelist", "SetWhitelistBundles", "SetUrlRules",
    "SetBlockedCategories",
    "ApplyProfile", "SaveProfile", "DeleteProfile", "ApproveRequest",
    "DismissRequest", "AllowUrl", "EditList", "SetMediaLevel",
    "SetLanguageFilter", "SetYouTube", "SetUserAdmin", "SetLayout",
    "SetCoverStyle", "SetAdBlock", "SetTimeLimits", "SetGuestConfig", "CreateUser",
    "AdoptUser", "RemoveUser", "SetUserApps", "RemoveApp", "ApproveApp",
    "UnapproveApp", "SetUserCanInstall", "SetCaptiveMode",
    "SetGuardianPassword", "DisableGuardian", "Enrol", "Unenrol",
    # Moving the system in either direction. The log answers "who changed
    # this machine, and when", and changing which OS image it runs is
    # exactly that — a rollback especially, since it can restore an older
    # filter. Neither takes arguments, so only the actor and the time are
    # recorded.
    "ApplyUpdate", "Rollback",
})
# Changes whose first argument is the account they are about.
PER_USER = frozenset({
    "SetFilterMode", "SetWhitelist", "SetWhitelistBundles", "SetUrlRules",
    "SetBlockedCategories",
    "ApplyProfile", "SaveProfile", "AllowUrl", "SetMediaLevel",
    "SetLanguageFilter", "SetYouTube", "SetUserAdmin", "SetLayout",
    "SetCoverStyle", "SetTimeLimits", "RemoveUser", "SetUserApps",
    "SetUserCanInstall", "SetCaptiveMode",
})
# Changes whose arguments are secrets or too big to be worth keeping.
NO_ARGS_LOGGED = frozenset({"SetGuardianPassword", "DisableGuardian",
                            "Enrol", "EditList", "SetWhitelist",
                            "SetUrlRules"})

SETUP_CLOSED = "initial setup is already complete"
UNKNOWN_METHOD = "unknown method"


@dataclass(frozen=True)
class Requirement:
    """What a call must satisfy before it may run."""

    polkit_action: str | None = None
    needs_guardian: bool = False
    refusal: str | None = None

    @property
    def allowed(self) -> bool:
        return self.refusal is None


def evaluate(
    method: str,
    *,
    setup_complete: bool,
    session_unlocked: bool,
    guardian_enabled: bool,
    guardian_proven: bool,
) -> Requirement:
    """Decide what `method` needs from this caller."""
    if method not in ACTIONS:
        return Requirement(refusal=UNKNOWN_METHOD)

    if method in SETUP_METHODS:
        # Nobody could authorize the first-boot wizard: no admin exists yet.
        # Reading whether setup is done stays available forever.
        if method not in SETUP_READS and setup_complete:
            return Requirement(refusal=SETUP_CLOSED)
        return Requirement()

    # One prompt per session; Unlock itself always goes through polkit.
    polkit_action = None
    if method == "Unlock" or not session_unlocked:
        polkit_action = ACTIONS[method]

    needs_guardian = (
        method in GUARDIAN_GATED and guardian_enabled and not guardian_proven
    )
    return Requirement(polkit_action=polkit_action, needs_guardian=needs_guardian)
