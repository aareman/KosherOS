#!/usr/bin/env bash
# Does a filtered user's web traffic actually end up in the proxy?
#
# The firewall check proved the ruleset stops people. The service check
# proved the addon filters what it is given. Neither proved the middle
# step: that nftables actually diverts a filtered account's connections
# into mitmproxy, and does not divert anyone else's. That is one nat rule
# and everything downstream depends on it.
#
#   just check-redirect
set -uo pipefail

fail=0
say() { printf '  %-46s %s\n' "$1" "$2"; }
want() {
    if [ "$2" = "$3" ]; then say "$1" "ok"; else
        say "$1" "FAILED (wanted '$2', got '$3')"; fail=1
    fi
}

TARGET="${KOSHER_TEST_TARGET:?set to the origin container address}"

useradd -M -s /usr/sbin/nologin t-filtered 2>/dev/null
useradd -M -s /usr/sbin/nologin t-dnsfilter 2>/dev/null
FILTERED=$(id -u t-filtered)
PLAIN=$(id -u t-dnsfilter)
MITM=$(id -u kosher-mitm)

# Refuse to judge anything before the origin is reachable — a check that
# cannot tell "intercepted" from "unreachable" reports the same either way.
if ! curl -s --max-time 5 -o /dev/null "http://$TARGET/"; then
    echo "  the origin at $TARGET is unreachable before any rules load."
    echo "  Harness fault; nothing below would mean anything."
    exit 1
fi
say "the origin is reachable before any rules load" "ok"

install -d -m 0750 -o kosher-mitm -g kosher-mitm /var/lib/kosher-mitm
cat > /var/lib/kosher-mitm/rules.json <<EOF
{"$FILTERED": {"rules": [{"action": "block", "pattern": "$TARGET/*"}],
   "blocked_categories": [], "media_level": "none",
   "language_filter": "off", "youtube": {}}}
EOF
chmod 644 /var/lib/kosher-mitm/rules.json

setpriv --reuid="$MITM" --regid="$MITM" --clear-groups \
    mitmdump --mode transparent --listen-host 0.0.0.0 --listen-port 8080 \
        --set confdir=/var/lib/kosher-mitm --set block_global=false \
        --set termlog_verbosity=warn \
        --scripts /usr/share/kosher/mitm/kosher_filter.py \
        > /tmp/mitm.log 2>&1 &
for _ in $(seq 1 30); do
    (echo > /dev/tcp/127.0.0.1/8080) 2>/dev/null && break; sleep 1
done

cat > /tmp/policy.json <<EOF
{"schema_version": 1, "revision": 1, "source": "local",
 "guardian": {"enabled": false},
 "users": [
   {"uid": $FILTERED, "username": "t-filtered", "mode": "filtered"},
   {"uid": $PLAIN, "username": "t-dnsfilter", "mode": "dnsfilter"}
 ]}
EOF
kosherctl render-nft /tmp/policy.json > /tmp/kosher.nft
grep -q "redirect to :8080" /tmp/kosher.nft \
    && say "the ruleset contains a redirect at all" "ok" \
    || { say "the ruleset contains a redirect at all" "FAILED"; fail=1; }
nft -f /tmp/kosher.nft || { echo "  the ruleset does not load"; exit 1; }

# Our block page is unmistakable, so seeing it proves the whole chain:
# redirected, intercepted, rules applied.
fetch() {
    setpriv --reuid="$(id -u "$1")" --regid=0 --clear-groups \
        curl -s --max-time 8 "http://$TARGET/" 2>/dev/null
}
code() {
    setpriv --reuid="$(id -u "$1")" --regid=0 --clear-groups \
        curl -s --max-time 8 -o /dev/null -w '%{http_code}' \
        "http://$TARGET/" 2>/dev/null
}

echo "the redirect:"
want "a filtered account lands in the proxy" "blocked" \
    "$(fetch t-filtered | grep -qi 'This page is blocked' && echo blocked || echo no)"
want "and gets the block page, not a dead connection" "403" "$(code t-filtered)"

echo "everyone else:"
# dnsfilter is not inspected: its traffic must reach the origin untouched.
want "an uninspected account is not diverted" "yes" \
    "$(fetch t-dnsfilter | grep -q . && echo yes || echo no)"
want "and is not shown a block page" "200" "$(code t-dnsfilter)"

echo "the proxy itself:"
# Without the exemption the proxy's own upstream request is redirected
# back into the proxy, which loops until something gives up.
want "its own traffic is exempt from the redirect" "yes" \
    "$(setpriv --reuid="$MITM" --regid="$MITM" --clear-groups \
        curl -s --max-time 8 -o /dev/null -w '%{http_code}' \
        "http://$TARGET/" 2>/dev/null | grep -q 200 && echo yes || echo no)"

if grep -qiE "traceback" /tmp/mitm.log; then
    echo "  the addon raised:"; grep -iA3 traceback /tmp/mitm.log | head -8
    fail=1
fi

echo
[ "$fail" = 0 ] && echo "all redirect checks passed" || echo "REDIRECT CHECKS FAILED"
exit "$fail"
