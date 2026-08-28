#!/usr/bin/env bash
# Policy survives restarts, and enforcement is rebuilt from it.
set -uo pipefail
. "$(dirname "$0")/lib.sh"

ensure_test_users
uid=$(id -u dnskid)

section "Revisions"
before=$(kosherctl get-policy | python3 -c 'import json,sys; print(json.load(sys.stdin)["revision"])')
kctl set-mode "$uid" whitelist >/dev/null
after=$(kosherctl get-policy | python3 -c 'import json,sys; print(json.load(sys.stdin)["revision"])')
[ "$after" -gt "$before" ] && ok "every change bumps the revision (portal sync depends on it)" \
    || bad "revision did not advance ($before -> $after)"

check_contains "the policy on disk is root-only" "600" \
    stat -c '%a' /var/lib/kosher/policy.json

section "Across a daemon restart"
systemctl restart kosherd
sleep 2
check "the daemon comes back" 0 systemctl is-active --quiet kosherd
check_contains "the change persisted" "whitelist" \
    bash -c "kosherctl get-policy | python3 -c 'import json,sys; print([u[\"mode\"] for u in json.load(sys.stdin)[\"users\"] if u[\"uid\"]==$uid][0])'"
check_contains "enforcement is rebuilt from the policy" "$uid : jump mode_whitelist" \
    nft list chain inet kosher output

section "Sessions do not survive a restart"
# Sessions live in memory only, so a restarted daemon re-locks everyone.
check_contains "the admin session is closed again" "False" \
    python3 -c "from kosherd.client import DaemonClient; print(DaemonClient().session_status()[0])"

kctl set-mode "$uid" dnsfilter >/dev/null
report
