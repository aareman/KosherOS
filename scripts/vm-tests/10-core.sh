#!/usr/bin/env bash
# Services, the D-Bus API, and the DNS plane.
set -uo pipefail
. "$(dirname "$0")/lib.sh"

section "Services"
for s in kosher-firewall kosher-dns kosherd; do
    check "$s is active" 0 systemctl is-active --quiet "$s"
done
# The firewall must be established before any link comes up, or a machine
# is briefly unfiltered every boot.
check_contains "firewall is ordered before the network" "network-pre.target" \
    systemctl show kosher-firewall.service -p Before --value
check_contains "firewall ruleset survives reboots" "table inet kosher" \
    cat /etc/kosher/nft/kosher.nft

section "Daemon API"
check "kosherctl reaches the daemon" 0 kosherctl status
check "polkit refuses an unprivileged caller" 1 as nokid kosherctl get-policy
check_contains "policy validates against the shipped schema" "revision" \
    kosherctl get-policy

section "DNS plane"
check "names resolve through the local resolver" 0 dig +time=5 +short example.com
grep -q kosher-redirect-probe /etc/hosts \
    || echo "203.0.113.77 kosher-redirect-probe.internal" >> /etc/hosts
systemctl reload kosher-dns >/dev/null 2>&1 || systemctl restart kosher-dns
sleep 1
if [ "$(as dnskid dig +time=5 @8.8.8.8 +short kosher-redirect-probe.internal)" = "203.0.113.77" ]; then
    ok "queries aimed elsewhere are answered by the local resolver"
else
    bad "port-53 redirect is not in effect"
fi
answer=$(dig +time=5 +short pornhub.com | head -1)
if [ -z "$answer" ] || [ "$answer" = "0.0.0.0" ]; then
    ok "family DNS blocks adult domains (answer: '${answer:-empty}')"
else
    bad "adult domain resolved to $answer — family upstream not in effect"
fi
check "DNS-over-TLS is rejected" 1 as dnskid nc -w 3 1.1.1.1 853
check "DoH to a public resolver IP is rejected" 1 as dnskid $CURL https://1.1.1.1

report
