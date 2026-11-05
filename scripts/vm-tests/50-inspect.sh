#!/usr/bin/env bash
# Inspect mode: TLS interception applying per-user URL rules.
set -uo pipefail
. "$(dirname "$0")/lib.sh"

ensure_test_users
uid=$(id -u wlkid)

section "Turning it on"
# "inspect" was the mode's first name; the CLI takes the current one.
check "mode can be set to filtered" 0 kosherctl set-mode "$uid" filtered --guardian-password ""
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

# Compare the TLS signer with this guest's CA, not branding in the body:
# an outer inspection proxy may legitimately add its own KosherOS script.
# First prove that the probe recognizes this guest's filtered connection.
guest_ca=/etc/pki/ca-trust/source/anchors/kosheros-inspect-ca.crt
probe_dir=$(mktemp -d)
chmod 0755 "$probe_dir"
for who in wlkid dnskid; do
    if timeout 20 runuser -u "$who" -- openssl s_client \
        -connect example.com:443 -servername example.com -showcerts \
        </dev/null >"$probe_dir/$who.chain" 2>"$probe_dir/$who.log" \
        && openssl x509 -in "$probe_dir/$who.chain" \
            -out "$probe_dir/$who.pem" 2>/dev/null; then
        ok "$who receives a readable TLS certificate"
    else
        bad "$who TLS signer probe failed"
    fi
done
check "the filtered connection is signed by this guest's CA" 0 \
    openssl verify -no-CApath -no-CAstore -CAfile "$guest_ca" "$probe_dir/wlkid.pem"
code=$(http_code_as dnskid https://example.com/blocked/page /tmp/other.html)
if [ "$code" != "000" ] && [ "$code" != "403" ] \
   && openssl x509 -in "$probe_dir/dnskid.pem" -noout >/dev/null 2>&1 \
   && ! openssl verify -no-CApath -no-CAstore -CAfile "$guest_ca" \
       "$probe_dir/dnskid.pem" >/dev/null 2>&1; then
    ok "uninspected user reaches upstream without this guest's TLS interception (HTTP $code)"
else
    bad "uninspected user failed the upstream/TLS isolation check (HTTP $code)"
fi
rm -rf "$probe_dir"

section "Turning it off"
kctl rules "$uid" clear >/dev/null
check "mode can be set back" 0 kosherctl set-mode "$uid" whitelist --guardian-password ""
sleep 2
# Only true when this was the last filtered account: a test VM that has
# accumulated others keeps the proxy up for them, which is correct.
if command -v nix >/dev/null 2>&1; then
    skip "the proxy stops when nobody is inspected (it runs wherever Nix is: the build users fetch through it, #123)"
elif kosherctl status 2>/dev/null | grep -v "^  wlkid " | grep -qE "\): filtered"; then
    skip "the proxy stops when nobody is inspected (another account on this VM is filtered)"
else
    check "the proxy stops when nobody is inspected" 1 systemctl is-active --quiet kosher-mitm
fi

report
