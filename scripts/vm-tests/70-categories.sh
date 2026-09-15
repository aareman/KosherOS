#!/usr/bin/env bash
# Content categories: the list is loaded, blocking reaches the right layer
# for the right users, and unblocking actually restores access.
#
# Note the choice of category: adult domains are blocked by the family
# resolver UPSTREAM, so they cannot show whether OUR blocking is on or off —
# an earlier version of this suite "failed" for exactly that reason.
set -uo pipefail
PASS=0; FAIL=0
ok()  { PASS=$((PASS+1)); echo "  PASS: $1"; }
bad() { FAIL=$((FAIL+1)); echo "  FAIL: $1"; }

id catkid >/dev/null 2>&1 || useradd -m catkid
kosherctl adopt catkid dnsfilter >/dev/null 2>&1 || \
  kosherctl set-mode "$(id -u catkid)" dnsfilter --guardian-password "" >/dev/null
uid=$(id -u catkid)

echo "== the list is loaded"
kosherctl categories available | head -3
if kosherctl categories available | grep -q adult; then
  ok "adult category available"
else
  bad "no categories"
fi

echo "== blocking a category for a dnsfilter user (DNS path)"
kosherctl categories "$uid" block social --guardian-password "" >/dev/null
sleep 3
if grep -q "address=/facebook.com/0.0.0.0" /etc/kosher/dnsmasq.d/categories.conf; then
  ok "the blocklist reached dnsmasq"
else
  bad "categories.conf missing the domain"
fi
ip=$(dig +short facebook.com | tail -1)
if [ "$ip" = "0.0.0.0" ]; then
  ok "a categorised domain resolves to 0.0.0.0"
else
  bad "resolved to '$ip'"
fi
good=$(dig +short chinuch.org | tail -1)
if [[ "$good" =~ ^[0-9]+\. ]] && [ "$good" != "0.0.0.0" ]; then
  ok "an uncategorised domain still resolves ($good)"
else
  bad "collateral: chinuch.org -> '$good'"
fi

echo "== unblocking restores it"
kosherctl categories "$uid" clear --guardian-password "" >/dev/null
sleep 3
ip=$(dig +short facebook.com | tail -1)
if [[ "$ip" =~ ^[0-9]+\. ]] && [ "$ip" != "0.0.0.0" ]; then
  ok "unblocking restores resolution ($ip)"
else
  bad "still blocked ('$ip')"
fi

echo "== filtered users are left to the proxy, not DNS"
kosherctl set-mode "$uid" filtered --guardian-password "" >/dev/null
kosherctl categories "$uid" block social --guardian-password "" >/dev/null
sleep 3
if grep -q "address=" /etc/kosher/dnsmasq.d/categories.conf; then
  bad "DNS is over-blocking for a filtered user"
else
  ok "no DNS blocks for a filtered user"
fi
if python3 -c "
import json
d = json.load(open('/var/lib/kosher-mitm/rules.json'))
print('  proxy sees:', d.get('$uid'))
raise SystemExit(0 if 'social' in d.get('$uid', {}).get('blocked_categories', []) else 1)
"; then
  ok "the proxy was handed the category"
else
  bad "proxy file missing the category"
fi

echo "RESULT: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
