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
# Granted to every active local user: changing a setting about YOURSELF
# that is a preference, not a protection (today: your own desktop layout,
# from the Look app). The filter is per-uid at the network layer whatever
# draws the windows, so nothing here loosens it.
ACTION_SET_OWN = "org.kosherlinux.set-own-desktop"
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
    # The same choice, made by the person for themselves in the Look app.
    "SetMyLayout": ACTION_SET_OWN,
    # And which compositor their advanced desktop runs on.
    "GetMyCompositor": ACTION_READ_OWN,
    "SetMyCompositor": ACTION_SET_OWN,
    # Your own key remaps (keyd), from your own file. A preference about
    # your own keyboard: it is in force only while your session is on the
    # screen, and a command() key runs as you, never as root (kosherd
    # never hands keyd your text). See keyboard.py.
    "SetMyKeyboard": ACTION_SET_OWN,
    # Per-application keys from your own app.conf, the same way: checked
    # like your files, and only while your session is on the screen.
    "SetMyAppKeys": ACTION_SET_OWN,
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
    # Beyond the web: video calls (UDP the filter cannot read) and extra
    # ports. Opening either widens what the account reaches.
    "SetNetworkAccess": ACTION_MANAGE_FILTER,
    # SSH to other machines (#257): a tunnel the filter cannot read, to
    # the listed machines or anywhere. Widens what the account reaches.
    "SetSsh": ACTION_MANAGE_FILTER,
    # Developer tools: the Nix daemon and the registries, directly. On
    # widens what the account reaches, so dual-controlled like a filter.
    "SetDeveloperTools": ACTION_MANAGE_FILTER,
    # Where a Nix build may download from, beyond the built-in hosts.
    "SetBuildSources": ACTION_MANAGE_FILTER,
    # The boot menu: the way round every other protection, so setting its
    # password is dual-controlled like a filter change; reading whether one
    # is set is a read.
    "SetBootMenuPassword": ACTION_MANAGE_FILTER,
    "BootMenuProtected": ACTION_READ_CONFIG,
    "ApproveRequest": ACTION_MANAGE_FILTER,
    "DismissRequest": ACTION_MANAGE_USERS,
    "ListRequests": ACTION_READ_CONFIG,
    # Allowing a blocked page from the activity view is the same grant as
    # approving a request for it.
    "AllowUrl": ACTION_MANAGE_FILTER,
    "ListActivity": ACTION_READ_CONFIG,
    "ActivitySummary": ACTION_READ_CONFIG,
    "Usage": ACTION_READ_CONFIG,
    "SetMediaLevel": ACTION_MANAGE_FILTER,
    "SetLanguageFilter": ACTION_MANAGE_FILTER,
    "SetYouTube": ACTION_MANAGE_FILTER,
    # The calmer addictive sites (#179): a filter setting like any other.
    "SetCalm": ACTION_MANAGE_FILTER,
    # Finding a channel to approve reads nothing of the machine's and
    # changes nothing; approving it is SetYouTube, which is gated.
    "SearchYouTubeChannels": ACTION_READ_CONFIG,
    "ListProfiles": ACTION_READ_CONFIG,
    "FilterStatus": ACTION_READ_CONFIG,
    "GetListEdits": ACTION_READ_CONFIG,
    "EditList": ACTION_MANAGE_FILTER,
    "ListCategories": ACTION_READ_CONFIG,
    "SetGuestConfig": ACTION_MANAGE_FILTER,
    "CreateUser": ACTION_MANAGE_USERS,
    "AdoptUser": ACTION_MANAGE_USERS,
    "RemoveUser": ACTION_MANAGE_USERS,
    # Account management, like creating one: it hands nobody a password and
    # cannot touch an administrator's (see impl_ResetPassword), so it is not
    # guardian-gated — a parent should not need the other parent present to
    # let a child back into their own account.
    "ResetPassword": ACTION_MANAGE_USERS,
    # The app store is for everyone: browsing and installing from the
    # pre-approved catalog need no admin password (kosherd still checks the
    # per-user can_install_apps flag). Removing affects every user, so it
    # stays an admin action.
    "ListCatalog": ACTION_USE_STORE,
    # What the Store shows the CALLER: the approved list, or the whole
    # store minus what is blocked for them (appaccess.py).
    "ListStoreApps": ACTION_USE_STORE,
    "ListInstalled": ACTION_USE_STORE,
    "InstallApp": ACTION_USE_STORE,
    # An administrator installing for an account: an admin's action, not
    # the store's, which is open to the account alone.
    "InstallAppFor": ACTION_INSTALL_APPS,
    "ListInstalledDetails": ACTION_READ_CONFIG,
    # The same list as the Store shows it to the CALLER: every Flatpak
    # (system-wide, which `flatpak list` already shows anyone) and the
    # caller's own nixpkgs apps, with nothing about who installed what.
    # ListInstalledDetails stays an admin read because it names the
    # accounts and lists every account's own apps.
    "ListMyInstalledDetails": ACTION_USE_STORE,
    # Updating an installed, approved app is for everyone the store is for:
    # it widens nothing, and the newer build is the safer one to be on.
    "ListAppUpdates": ACTION_USE_STORE,
    "CheckAppUpdates": ACTION_USE_STORE,
    "UpdateApp": ACTION_USE_STORE,
    "UpdateAllApps": ACTION_USE_STORE,
    "RemoveApp": ACTION_INSTALL_APPS,
    # Leftover runtimes: anyone the store is for may see what they take;
    # removing is an administrator's action like removing an app.
    "ListUnusedApps": ACTION_USE_STORE,
    "RemoveUnusedApps": ACTION_INSTALL_APPS,
    "SetUserApps": ACTION_INSTALL_APPS,
    "SetUserCanInstall": ACTION_INSTALL_APPS,
    # Opening an account to the whole store, or lifting a block, widens
    # what it can reach, so these are guardian-gated like the web filter.
    "SetUserAppAccess": ACTION_INSTALL_APPS,
    "SetUserBlockedAppKinds": ACTION_INSTALL_APPS,
    "SetUserBlockedApps": ACTION_INSTALL_APPS,
    # Syncthing for one account (#213): files from its paired devices
    # never pass the filter, and its ports go out directly. Approving it,
    # and letting the home network reach it, are guardian-gated.
    "SetUserAppApproval": ACTION_INSTALL_APPS,
    "SetSyncthingLan": ACTION_MANAGE_FILTER,
    # Syncthing for the whole computer (#220): off, nobody has it and the
    # Store does not offer it. On, an administrator's account and an
    # unfiltered one may install it with no approval, so it is gated.
    "SetSyncthingEnabled": ACTION_MANAGE_FILTER,
    # Key remapping for the whole computer (#230): keyd is a root daemon
    # that reads every keyboard, and a remapped key can run a command in
    # the person's session. Gated like the other Advanced switches.
    "SetKeydEnabled": ACTION_MANAGE_FILTER,
    "SearchApps": ACTION_READ_CONFIG,
    "ApproveApp": ACTION_INSTALL_APPS,
    "UnapproveApp": ACTION_INSTALL_APPS,
    "CheckUpdate": ACTION_READ_CONFIG,
    "FilterLog": ACTION_READ_CONFIG,
    "ApplyUpdate": ACTION_APPLY_UPDATES,
    "DeploymentStatus": ACTION_READ_CONFIG,
    "ListChannels": ACTION_READ_CONFIG,
    # Which stream of builds this computer follows. Guardian-gated: moving
    # the family computer onto edge puts it on builds nobody has tried
    # yet, and that is a decision for whoever holds the second password.
    "SetChannel": ACTION_APPLY_UPDATES,
    # Going back to the previously booted image. Guardian-gated: the image
    # gone back to carries the filter it was built with, which can be older
    # and weaker than the one running now, so going back is a filter change
    # like any other (F11 on #33). The automatic return is not held up by
    # this: greenboot never calls kosherd, the boot counter takes a machine
    # whose filter did not come up back to the last good deployment by
    # itself. See impl_Rollback.
    "Rollback": ACTION_APPLY_UPDATES,
    # Trying a build that is on no channel (a pull request's) while
    # keeping the one the machine came from, and going back to it. The
    # first is gated like a channel switch, for the same reason; the
    # second is a rollback onto an image this machine already ran.
    "TryImage": ACTION_APPLY_UPDATES,
    "ReturnToKept": ACTION_APPLY_UPDATES,
    # The Restart button after an update. Same action as applying it: a
    # restart is the second half of the same job.
    "Reboot": ACTION_APPLY_UPDATES,
    # Automatic updates (#19). The schedule is a system setting like the
    # channel, but not guardian-gated: it only decides when updates the
    # administrator could fetch anyway are fetched, and "off" leaves the
    # Check button working. The run is the timer's (root) or an
    # administrator's "check now".
    "SetAutoUpdates": ACTION_APPLY_UPDATES,
    "AutoUpdateStatus": ACTION_READ_CONFIG,
    "RunAutoUpdate": ACTION_APPLY_UPDATES,
    # Whether an update waits for a restart, for the mark in every
    # person's panel. Says nothing about anybody's filter, and is answered
    # from kosherd's own note, so any account may ask without a password.
    "PendingUpdate": ACTION_READ_OWN,
    "SetCaptiveMode": ACTION_MANAGE_NETWORK,
    # Tailscale (#30). Reading whether this computer is on a tailnet is a
    # read. Signing it in puts it on a network with other people's
    # machines, which every account can then reach by name through its
    # own filter, so it is guardian-gated; signing out only takes that
    # away, and is not.
    "TailscaleStatus": ACTION_READ_CONFIG,
    "TailscaleSignIn": ACTION_MANAGE_NETWORK,
    "TailscaleSignOut": ACTION_MANAGE_NETWORK,
    # Whether tailscaled runs at all (#220). Starting it opens nothing
    # until it is signed in, which is gated on its own; it is gated anyway
    # so both of the Advanced switches ask the same way.
    "SetTailscaleEnabled": ACTION_MANAGE_NETWORK,
    "IsEnabled": ACTION_READ_CONFIG,
    "SetGuardianPassword": ACTION_MANAGE_GUARDIAN,
    "DisableGuardian": ACTION_MANAGE_GUARDIAN,
}

# Methods that can weaken the filter: guardian password required when enabled.
GUARDIAN_GATED = frozenset({
    "SetFilterMode", "SetWhitelist", "SetWhitelistBundles", "SetUrlRules", "SetBlockedCategories", "ApplyProfile", "SaveProfile", "DeleteProfile", "ApproveRequest", "AllowUrl", "EditList",
    "SetUserAdmin",
    "SetMediaLevel",
    "SetLanguageFilter", "SetYouTube", "SetCalm",
    "SetGuestConfig", "DisableGuardian", "Enrol", "Unenrol",
    "SetAdBlock", "SetTimeLimits", "SetChannel", "TryImage", "Rollback", "SetNetworkAccess", "SetSsh",
    "SetDeveloperTools", "SetBuildSources",
    "SetBootMenuPassword",
    "SetUserAppAccess", "SetUserBlockedAppKinds", "SetUserBlockedApps",
    "SetUserAppApproval", "SetSyncthingLan", "SetSyncthingEnabled",
    "TailscaleSignIn", "SetTailscaleEnabled", "SetKeydEnabled",
})

# First-boot only; closed forever once setup is stamped complete.
SETUP_METHODS = frozenset({"IsComplete", "AdminExists", "ExistingAccounts",
                           "CreateFirstAdmin", "FinishSetup",
                           "NetworkStatus", "ListWifi", "ConnectWifi"})
# Readable forever: the wizard and the apps ask these on every start.
SETUP_READS = frozenset({"IsComplete", "AdminExists"})

# Methods that need to know which uid called them (session management).
UID_AWARE = frozenset({"Unlock", "Lock", "Status", "VerifyGuardian", "InstallApp",
                       "ListStoreApps", "ListMyInstalledDetails", "GetMyLayout", "SetMyLayout", "GetMySettings",
                       "GetMyCompositor", "SetMyCompositor",
                       "SetMyKeyboard", "SetMyAppKeys",
                       # A nixpkgs app is in one account's profile: what is
                       # installed, and whose copy to update or remove,
                       # depends on who is asking.
                       "ListInstalled", "UpdateApp", "RemoveApp"})

# Calls that change how the machine is set up, written to the activity log
# with the admin who made them so "who changed this?" has an answer. Reads,
# session calls and the store's own installs are not changes to the family's
# settings and are not logged.
CHANGES = frozenset({
    "SetFilterMode", "SetWhitelist", "SetWhitelistBundles", "SetUrlRules",
    "SetBlockedCategories",
    "ApplyProfile", "SaveProfile", "DeleteProfile", "ApproveRequest",
    "DismissRequest", "AllowUrl", "EditList", "SetMediaLevel",
    "SetLanguageFilter", "SetYouTube", "SetCalm", "SetUserAdmin", "SetLayout",
    "SetCoverStyle", "SetAdBlock", "SetTimeLimits", "SetNetworkAccess", "SetDeveloperTools",
    "SetBuildSources",
    "SetBootMenuPassword", "SetGuestConfig", "CreateUser",
    "AdoptUser", "RemoveUser", "ResetPassword", "SetUserApps", "RemoveApp", "RemoveUnusedApps",
    "InstallAppFor", "ApproveApp",
    "UnapproveApp", "SetUserCanInstall", "SetUserAppAccess",
    "SetUserBlockedAppKinds", "SetUserBlockedApps", "SetCaptiveMode",
    "SetUserAppApproval", "SetSyncthingLan", "SetSyncthingEnabled",
    "SetGuardianPassword", "DisableGuardian", "Enrol", "Unenrol",
    # Moving the system in either direction. The log answers "who changed
    # this machine, and when", and changing which OS image it runs is
    # exactly that — a rollback especially, since it can restore an older
    # filter. Neither takes arguments, so only the actor and the time are
    # recorded.
    "ApplyUpdate", "Rollback",
    # And which stream of builds it follows from now on, which is the
    # longest-lived of the three.
    "SetChannel",
    # And how often it fetches updates on its own.
    "SetAutoUpdates",
    # A test build put on the machine, and the return from it.
    "TryImage", "ReturnToKept",
    # Joining a tailnet and leaving it.
    "TailscaleSignIn", "TailscaleSignOut", "SetTailscaleEnabled",
    # And key remapping switched on or off.
    "SetKeydEnabled",
})
# Changes whose first argument is the account they are about.
PER_USER = frozenset({
    "SetFilterMode", "SetWhitelist", "SetWhitelistBundles", "SetUrlRules",
    "SetBlockedCategories",
    "ApplyProfile", "SaveProfile", "AllowUrl", "SetMediaLevel",
    "SetLanguageFilter", "SetYouTube", "SetCalm", "SetUserAdmin", "SetLayout",
    "SetCoverStyle", "SetTimeLimits", "SetNetworkAccess", "SetDeveloperTools", "InstallAppFor",
    "RemoveUser", "ResetPassword", "SetUserApps",
    "SetUserCanInstall", "SetUserAppAccess", "SetUserBlockedAppKinds",
    "SetUserBlockedApps", "SetCaptiveMode", "SetUserAppApproval", "SetSyncthingLan",
})
# Changes whose arguments are secrets or too big to be worth keeping.
NO_ARGS_LOGGED = frozenset({"SetGuardianPassword", "DisableGuardian", "SetBootMenuPassword",
                            "Enrol", "EditList", "SetWhitelist",
                            "SetUrlRules"})

# Actions every active local account has without a password
# (49-kosher-admin.rules answers yes to these before it asks for one).
FREE_ACTIONS = frozenset({ACTION_USE_STORE, ACTION_READ_OWN, ACTION_SET_OWN})


def needs_admin_session(method: str) -> bool:
    """Would `method` ask a non-administrator for an administrator's
    password when no session is open?

    polkit keeps an answered prompt per action, so a command that read a
    setting and then changed one asked twice. A client about to make such
    calls opens the session first with Unlock, which is the one prompt;
    kosherd then skips polkit for that uid until the session ends. The gate
    is the same either way: Unlock takes an administrator's password just
    as the read and the change do.
    """
    if method not in ACTIONS or method in SETUP_METHODS:
        return False
    if method in ("Unlock", "Lock"):
        return False
    return ACTIONS[method] not in FREE_ACTIONS


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
