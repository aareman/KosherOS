#!/usr/bin/env bash
# The per-user enforcement matrix, plus fail-closed behaviour.
set -uo pipefail
. "$(dirname "$0")/lib.sh"

ensure_test_users
kctl set-mode "$(id -u wlkid)" whitelist >/dev/null
kctl set-whitelist "$(id -u wlkid)" example.com >/dev/null
kctl set-mode "$(id -u nokid)" none >/dev/null
kctl set-mode "$(id -u dnskid)" dnsfilter >/dev/null
wait_for_dns  # the resolver restarts on every policy change
sleep 1       # ...and then repopulates the whitelist sets

section "Filter modes"
check "root is unrestricted"                    0 fetch https://fedoraproject.org
check "no-internet user is blocked"             1 fetch_as nokid https://example.com
check "dns-filtered user browses"               0 fetch_as dnskid https://example.com
check "whitelisted site loads"                  0 fetch_as wlkid https://example.com
check "non-whitelisted site is blocked"         1 fetch_as wlkid https://www.wikipedia.org
# A live address of a site that is NOT on the whitelist, over plain HTTP:
# the old fixed 93.184.216.34 stopped answering, and https to a bare
# address fails on the certificate anyway, so this passed for two wrong
# reasons at once.
other=$(resolve4 wikipedia.org)
check "direct-IP browsing is blocked (http://$other/)" 1 fetch_as wlkid "http://$other/"
check_contains "whitelist set is populated"     "elements" nft list set inet kosher wl4

section "Fail closed"
# A human account nobody has adopted must not get a free pass.
id unmanaged >/dev/null 2>&1 || useradd -m unmanaged
check "an unmanaged account has no internet" 1 fetch_as unmanaged https://example.com

section "Containment"
# A user namespace has no route out except a proxy owned by that same user,
# so the per-uid rules still apply.
check "user namespaces do not escape the filter" 1 \
    as nokid unshare -rn -- curl -sS --max-time 8 -o /dev/null https://example.com

section "Ad blocking, for everyone"
# Rendered into both resolvers, so the unfiltered account's plain resolver
# blocks the same domains as the family one.
check "the ad list is rendered for the family resolver" 0 \
    grep -q '^address=/' /etc/kosher/dnsmasq.d/adblock.conf
check "and identically for the plain resolver" 0 \
    cmp -s /etc/kosher/dnsmasq.d/adblock.conf /etc/kosher/dnsmasq-open.d/adblock.conf
check "a known ad domain does not resolve" 1 \
    dig +short +time=3 @127.0.0.1 doubleclick.net A

section "Captive portal"
check "a temporary window can be opened" 0 kosherctl captive "$(id -u nokid)" 1
check "the window lets that user out"    0 fetch_as nokid https://example.com

report
