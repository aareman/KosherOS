#!/usr/bin/env bash
# Inspect mode: TLS interception applying per-user URL rules.
set -uo pipefail
. "$(dirname "$0")/lib.sh"

ensure_test_users
uid=$(id -u wlkid)

section "Turning it on"
check "mode can be set to inspect" 0 kosherctl set-mode "$uid" inspect --guardian-password ""
kctl rules "$uid" clear >/dev/null
check "a block rule can be added" 0 \
    kosherctl rules "$uid" block "example.com/blocked*" --guardian-password ""
check_contains "bad patterns are rejected before they reach the proxy" "action" \
    kosherctl rules "$uid" bogus "x.com" --guardian-password ""
wait_for_dns  # every policy change restarts the resolver
sleep 2       # ...and the proxy needs a moment to bind

section "Plumbing"
check "the proxy is running" 0 systemctl is-active --quiet kosher-mitm
check "the inspection CA is in the system trust store" 0 \
    test -f /etc/pki/ca-trust/source/anchors/kosheros-inspect-ca.crt
check_contains "rules are rendered for the proxy" "blocked" \
    cat /var/lib/kosher-mitm/rules.json
check "Firefox is told to honour system roots" 0 test -f /etc/firefox/policies/policies.json
check_contains "web traffic is redirected into the proxy" "redirect to :3" \
    nft list chain inet kosher dns_redirect
# Each filtered user has their own listener; the proxy must be on all of them.
check_contains "the proxy listens on the per-user ports" "transparent@127.0.0.1:30000" \
    cat /var/lib/kosher-mitm/listen.env
# The proxy must never read the policy itself.
check "the proxy cannot read the policy" 1 as kosher-mitm cat /var/lib/kosher/policy.json

section "Filtering in practice"
code=$(http_code_as wlkid https://example.com/blocked/page /tmp/blocked.html)
if [ "$code" = "403" ] && grep -qi "blocked" /tmp/blocked.html; then
    ok "a blocked URL returns the KosherOS block page"
else
    bad "blocked URL returned '$code' (expected 403)"
fi
code=$(http_code_as wlkid https://example.com/)
assert_that "an allowed URL loads with TLS trusted (got $code)" \
    test "$code" = "200"

code=$(http_code_as dnskid https://example.com/blocked/page /tmp/other.html)
# "000" would mean the request never completed, which proves nothing — the
# uninspected user must actually reach the real server.
if [ "$code" != "000" ] && [ "$code" != "403" ] \
   && ! grep -qi "KosherOS" /tmp/other.html 2>/dev/null; then
    ok "users not in inspect mode reach the real server (HTTP $code)"
else
    bad "uninspected user got '$code' (expected a real response)"
fi

section "Turning it off"
kctl rules "$uid" clear >/dev/null
check "mode can be set back" 0 kosherctl set-mode "$uid" whitelist --guardian-password ""
sleep 2
check "the proxy stops when nobody is inspected" 1 systemctl is-active --quiet kosher-mitm

report
