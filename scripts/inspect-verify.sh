#!/usr/bin/env bash
# Inspect-mode verification: run as root in the dev VM after dev-install.
# Proves TLS interception applies per-user URL rules.
set -uo pipefail
PASS=0; FAIL=0
ok()  { PASS=$((PASS+1)); echo "  PASS: $1"; }
bad() { FAIL=$((FAIL+1)); echo "  FAIL: $1"; }

uid=$(id -u wlkid)

echo "== enabling inspect mode for wlkid (uid $uid)"
kosherctl set-mode "$uid" inspect --guardian-password "" >/dev/null \
    && ok "mode set to inspect" || bad "could not set inspect mode"
kosherctl rules "$uid" clear --guardian-password "" >/dev/null
kosherctl rules "$uid" block "example.com/blocked*" --guardian-password "" >/dev/null \
    && ok "block rule added" || bad "could not add rule"

echo "== service, CA and rules file"
sleep 2
systemctl is-active --quiet kosher-mitm && ok "kosher-mitm running" \
    || { bad "kosher-mitm not running"; journalctl -u kosher-mitm -n 5 --no-pager | tail -3; }
[ -f /etc/pki/ca-trust/source/anchors/kosheros-inspect-ca.crt ] \
    && ok "inspection CA installed in system trust" || bad "CA not installed"
grep -q "blocked" /var/lib/kosher-mitm/rules.json \
    && ok "rules rendered for the proxy" || bad "rules file missing the rule"
[ -f /etc/firefox/policies/policies.json ] \
    && ok "Firefox enterprise-roots policy written" || bad "Firefox policy missing"

echo "== nftables redirect"
nft list chain inet kosher dns_redirect | grep -q "redirect to :8080" \
    && ok "web traffic redirected into the proxy" || bad "no redirect rule"

echo "== interception in practice (as wlkid)"
# The proxy must be reachable and serving our block page for a blocked URL.
code=$(runuser -u wlkid -- curl -sS -o /tmp/blocked.html -w '%{http_code}' \
        --max-time 20 https://example.com/blocked/page 2>/dev/null)
if [ "$code" = "403" ] && grep -qi "blocked" /tmp/blocked.html; then
    ok "blocked URL returns the KosherOS block page (403)"
else
    bad "blocked URL returned '$code' (expected 403)"
    head -3 /tmp/blocked.html 2>/dev/null | sed 's/^/    /'
fi

code=$(runuser -u wlkid -- curl -sS -o /dev/null -w '%{http_code}' \
        --max-time 20 https://example.com/ 2>/dev/null)
[ "$code" = "200" ] && ok "allowed URL passes through (200, TLS trusted)" \
    || bad "allowed URL returned '$code' (expected 200)"

echo "== an uninspected user is untouched"
# Same path the inspected user is blocked from: this user must reach the
# real server (which happens to 404 it) rather than our block page.
code=$(runuser -u dnskid -- curl -sS -o /tmp/other.html -w '%{http_code}' \
        --max-time 20 https://example.com/blocked/page 2>/dev/null)
if [ "$code" != "403" ] && ! grep -qi "KosherOS" /tmp/other.html 2>/dev/null; then
    ok "dnsfilter user reaches the real site, not the block page (HTTP $code)"
else
    bad "dnsfilter user was intercepted (got '$code')"
fi

echo "== switching back off stops the proxy"
kosherctl set-mode "$uid" whitelist --guardian-password "" >/dev/null
sleep 2
systemctl is-active --quiet kosher-mitm && bad "proxy still running with nobody inspected" \
    || ok "proxy stopped when no user is inspected"

echo "RESULT: $PASS passed, $FAIL failed"
exit $([ "$FAIL" -eq 0 ] && echo 0 || echo 1)
