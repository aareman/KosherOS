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
assert_that "every change bumps the revision ($before -> $after; portal sync depends on it)" \
    test "$after" -gt "$before"

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

section "A burst of changes"
# Found by running the suites back to back: every change restarts the
# resolver, so six quick edits tripped systemd's start rate limit and left
# the machine with NO DNS until someone reset the unit by hand.
for _ in 1 2 3 4 5 6 7 8; do
    kctl set-mode "$uid" dnsfilter >/dev/null
    kctl set-mode "$uid" whitelist >/dev/null
done
check "the resolver survives rapid policy changes" 0 \
    systemctl is-active --quiet kosher-dns
assert_that "DNS still answers afterwards" wait_for_dns

kctl set-mode "$uid" dnsfilter >/dev/null
report
