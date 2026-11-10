# shellcheck shell=sh
# The same per-account desktop profile as the systemd user session.
# Do not replace GDM's profile or an unmanaged account's defaults.
kosher_desktop_profile="/etc/dconf/profile/kosher-$(id -u)"
if [ -r "$kosher_desktop_profile" ]; then
    export DCONF_PROFILE="$kosher_desktop_profile"
fi
unset kosher_desktop_profile
