#!/usr/bin/env bash
# The guest session: enable, enforce, wipe on sign-out, disable.
set -uo pipefail
. "$(dirname "$0")/lib.sh"

section "Enabling"
check "guest can be enabled" 0 kosherctl guest enable --mode whitelist chinuch.org --guardian-password ""
guid=$(id -u kosher-guest 2>/dev/null)
[ -n "$guid" ] && ok "the guest account exists (uid $guid)" || { bad "no guest account"; report; exit 1; }
[ -d /home/kosher-guest ] && ok "guest home exists" || bad "guest home missing"

state=$(passwd -S kosher-guest | awk '{print $2}')
[ "$state" = "NP" ] && ok "guest signs in without a password" || bad "guest passwd state is $state"

section "Enforcement"
check_contains "guest traffic is filtered like any user" "$guid : jump mode_whitelist" \
    nft list chain inet kosher output
if command -v malcontent-client >/dev/null 2>&1; then
    check_contains "guest cannot install apps" "installation is disallowed" \
        malcontent-client get-app-filter "$guid"
fi
check "guest cannot reach non-whitelisted sites" 1 \
    as kosher-guest $CURL https://www.wikipedia.org

section "Data is erased at sign-out"
touch /home/kosher-guest/leftover.txt
USER=kosher-guest bash /etc/gdm/PostSession/Default
if [ ! -e /home/kosher-guest/leftover.txt ] && [ -d /home/kosher-guest ]; then
    ok "the sign-out hook wipes and recreates the guest home"
else
    bad "guest data survived sign-out"
fi

section "Disabling"
check "guest can be disabled" 0 kosherctl guest disable --guardian-password ""
state=$(passwd -S kosher-guest | awk '{print $2}')
case "$state" in
    L|LK) ok "the guest account is locked when disabled" ;;
    *)    bad "guest passwd state is $state, expected locked" ;;
esac

report
