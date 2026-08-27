#!/usr/bin/env bash
# Guest-session verification: run as root in the dev VM.
set -uo pipefail
PASS=0; FAIL=0
ok()  { PASS=$((PASS+1)); echo "  PASS: $1"; }
bad() { FAIL=$((FAIL+1)); echo "  FAIL: $1"; }

kosherctl guest enable --mode whitelist chinuch.org --guardian-password "" >/dev/null \
    && ok "guest enable via D-Bus" || bad "guest enable failed"

guid=$(id -u kosher-guest 2>/dev/null)
[ -n "$guid" ] && ok "kosher-guest account exists (uid $guid)" || bad "no guest account"
[ -d /home/kosher-guest ] && ok "guest home exists" || bad "guest home missing"

state=$(passwd -S kosher-guest | awk '{print $2}')
[ "$state" = "NP" ] && ok "guest password empty (passwordless login)" || bad "guest passwd state: $state"

nft list chain inet kosher output | grep -q "$guid : jump mode_whitelist" \
    && ok "guest uid dispatched to whitelist chain" || bad "guest missing from nft vmap"

malcontent-client get-app-filter "$guid" | grep -qi "installation is disallowed" \
    && ok "guest flatpak installs disallowed" || bad "guest malcontent filter missing"

runuser -u kosher-guest -- curl -sS --max-time 8 -o /dev/null https://www.wikipedia.org 2>/dev/null \
    && bad "guest reached non-whitelisted site" || ok "guest blocked from non-whitelisted site"

touch /home/kosher-guest/secret.txt
USER=kosher-guest bash /etc/gdm/PostSession/Default
[ ! -e /home/kosher-guest/secret.txt ] && [ -d /home/kosher-guest ] \
    && ok "PostSession hook wipes and recreates guest home" || bad "guest wipe failed"

kosherctl guest disable --guardian-password "" >/dev/null \
    && ok "guest disable via D-Bus" || bad "guest disable failed"
state=$(passwd -S kosher-guest | awk '{print $2}')
[ "$state" = "L" ] || [ "$state" = "LK" ] && ok "guest login locked when disabled" || bad "guest not locked: $state"

echo "RESULT: $PASS passed, $FAIL failed"
exit $([ "$FAIL" -eq 0 ] && echo 0 || echo 1)
