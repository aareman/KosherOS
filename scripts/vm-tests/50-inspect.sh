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
sleep 3

section "Plumbing"
check "the proxy is running" 0 systemctl is-active --quiet kosher-mitm
check "the inspection CA is in the system trust store" 0 \
    test -f /etc/pki/ca-trust/source/anchors/kosheros-inspect-ca.crt
check_contains "rules are rendered for the proxy" "blocked" \
    cat /var/lib/kosher-mitm/rules.json
check "Firefox is told to honour system roots" 0 test -f /etc/firefox/policies/policies.json
check_contains "web traffic is redirected into the proxy" "redirect to :8080" \
    nft list chain inet kosher dns_redirect
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
if [ "$code" != "403" ] && ! grep -qi "KosherOS" /tmp/other.html 2>/dev/null; then
    ok "users not in inspect mode are never intercepted (HTTP $code)"
else
    bad "an uninspected user was intercepted (got '$code')"
fi

section "Turning it off"
kctl rules "$uid" clear >/dev/null
check "mode can be set back" 0 kosherctl set-mode "$uid" whitelist --guardian-password ""
sleep 2
check "the proxy stops when nobody is inspected" 1 systemctl is-active --quiet kosher-mitm

report
