#!/usr/bin/env bash
# Does the resolver actually do what the rendered config says?
#
# Same gap the firewall check closed, one layer down: unit tests prove the
# right lines are written into dnsmasq's config, and nothing proves
# dnsmasq then answers the way those lines claim. Safe search, category
# blocking and the whitelist sets are all only real if a query gets the
# answer we think it gets.
#
#   just check-dns
#
# The upstream is a second dnsmasq we control, so nothing here depends on
# the internet or on what a real domain happens to resolve to today.
set -uo pipefail

fail=0
say() { printf '  %-46s %s\n' "$1" "$2"; }
want() {
    if [ "$2" = "$3" ]; then say "$1" "ok"; else
        say "$1" "FAILED (wanted '$2', got '$3')"; fail=1
    fi
}
answer() { dig +short +time=2 +tries=1 -p "${2:-53}" @127.0.0.1 "$1" 2>/dev/null | head -1; }

useradd -M -s /usr/sbin/nologin t-wl 2>/dev/null
useradd -M -s /usr/sbin/nologin t-open 2>/dev/null
WL_UID=$(id -u t-wl)
OPEN_UID=$(id -u t-open)

# An upstream we own. Two records: one a whitelisted site resolves to, one
# a blocked one would, so no answer below depends on the real internet.
cat > /tmp/upstream.conf <<EOF
port=5355
listen-address=127.0.0.1
bind-interfaces
no-resolv
no-hosts
address=/chinuch.test/93.184.216.34
address=/forcesafesearch.google.com/216.239.38.120
address=/example.test/198.51.100.7
address=/blocked.test/203.0.113.9
EOF
dnsmasq --keep-in-foreground --conf-file=/tmp/upstream.conf > /tmp/upstream.log 2>&1 &
sleep 1

cat > /tmp/policy.json <<EOF
{"schema_version": 1, "revision": 1, "source": "local",
 "guardian": {"enabled": false},
 "users": [
   {"uid": $WL_UID, "username": "t-wl", "mode": "whitelist",
    "whitelist": ["chinuch.test"]},
   {"uid": $OPEN_UID, "username": "t-open", "mode": "unfiltered"},
   {"uid": 4242, "username": "t-dns", "mode": "dnsfilter",
    "blocked_categories": ["adult"]}
 ]}
EOF

mkdir -p /etc/kosher/dnsmasq.d
kosherctl render-dnsmasq /tmp/policy.json > /etc/kosher/dnsmasq.d/whitelist.conf || {
    echo "  could not render the whitelist config"; exit 1; }
# Category blocking, written the way apply.py writes it.
printf 'address=/blocked.test/0.0.0.0\naddress=/blocked.test/::\n' \
    > /etc/kosher/dnsmasq.d/categories.conf
python3 - > /etc/kosher/dnsmasq.d/safesearch.conf <<'PY'
import json
from kosherd.dns import render_safesearch
from kosherd.policy import Policy
print(render_safesearch(Policy.from_dict(json.load(open("/tmp/policy.json")))),
      end="")
PY

cat > /tmp/kosher-dns.conf <<EOF
port=53
listen-address=127.0.0.1
bind-interfaces
no-resolv
no-hosts
server=127.0.0.1#5355
conf-dir=/etc/kosher/dnsmasq.d,*.conf
EOF

dnsmasq --test --conf-file=/tmp/kosher-dns.conf 2>&1 | grep -q "syntax check OK" \
    && say "the rendered config passes dnsmasq's own check" "ok" \
    || { say "the rendered config passes dnsmasq's own check" "FAILED"; fail=1; }

# The firewall's sets must exist before dnsmasq can populate them.
nft add table inet kosher 2>/dev/null
nft add set inet kosher wl4 '{ type ipv4_addr; }' 2>/dev/null
nft add set inet kosher wl6 '{ type ipv6_addr; }' 2>/dev/null

dnsmasq --keep-in-foreground --conf-file=/tmp/kosher-dns.conf > /tmp/dns.log 2>&1 &
sleep 2

# Refuse to judge anything until the resolver answers at all — the lesson
# from the firewall check, where two harnesses reported confident nonsense.
if [ -z "$(answer chinuch.test)" ]; then
    echo "  the resolver does not answer at all, so nothing below would"
    echo "  mean anything. Harness fault:"
    tail -5 /tmp/dns.log | sed 's/^/    /'
    exit 1
fi
say "the resolver answers before anything is asserted" "ok"

echo "category blocking:"
want "a blocked domain answers 0.0.0.0" "0.0.0.0" "$(answer blocked.test)"
want "an ordinary domain is not touched" "198.51.100.7" "$(answer example.test)"

echo "safe search:"
want "google is redirected to its safe address" "216.239.38.120" \
    "$(dig +short +time=2 +tries=1 @127.0.0.1 www.google.com 2>/dev/null | tail -1)"
want "youtube gets restricted mode" "216.239.38.119" \
    "$(dig +short +time=2 +tries=1 @127.0.0.1 www.youtube.com 2>/dev/null | tail -1)"
want "bing too" "204.79.197.220" \
    "$(dig +short +time=2 +tries=1 @127.0.0.1 www.bing.com 2>/dev/null | tail -1)"

echo "the whitelist sets:"
nft flush set inet kosher wl4
before=$(nft list set inet kosher wl4 | grep -c "93.184.216.34")
answer chinuch.test > /dev/null
sleep 1
after=$(nft list set inet kosher wl4 | grep -c "93.184.216.34")
want "resolving a whitelisted name fills the firewall set" "0 1" "$before $after"
answer example.test > /dev/null
sleep 1
want "resolving anything else does not" "0" \
    "$(nft list set inet kosher wl4 | grep -c '198.51.100.7')"

echo "the open resolver, for unfiltered accounts:"
sed -e 's/^port=53$/port=5354/' -e '/conf-dir/d' /tmp/kosher-dns.conf > /tmp/open.conf
dnsmasq --keep-in-foreground --conf-file=/tmp/open.conf > /tmp/open.log 2>&1 &
sleep 2
want "it answers"                       "198.51.100.7" "$(answer example.test 5354)"
want "and applies none of the blocking" "203.0.113.9" "$(answer blocked.test 5354)"

echo
[ "$fail" = 0 ] && echo "all DNS checks passed" || echo "DNS CHECKS FAILED"
exit "$fail"
