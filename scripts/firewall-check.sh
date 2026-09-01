#!/usr/bin/env bash
# Load the real ruleset and see whether it actually stops anyone.
#
# Everything else tests the ruleset as TEXT: `nft --check` says it parses,
# and unit tests say the right lines are in it. Neither says a filtered
# user cannot reach the internet, which is the only claim that matters.
#
# So this loads it into a private network namespace, puts a web server on
# a non-loopback address, and tries to reach it as several users. Run with:
#   just check-firewall
#
# Not a substitute for a VM boot — DNS takeover, the login path and the
# transparent redirect still need a booted machine — but this is the
# security boundary itself, and it was the least exercised part.
set -uo pipefail

fail=0
say() { printf '  %-46s %s\n' "$1" "$2"; }
want() {
    if [ "$2" = "$3" ]; then say "$1" "ok"; else
        say "$1" "FAILED (wanted '$2', got '$3')"; fail=1
    fi
}

# The destination is another CONTAINER, on a shared podman network, so
# traffic to it genuinely leaves over eth0. Two earlier attempts did not:
#
#   a dummy interface with a local address — Linux routes packets to any
#   local address through `lo`, which the output chain accepts outright,
#   so every check passed and the test proved nothing;
#
#   a peer network namespace — `ip netns exec` cannot remount /sys in a
#   rootless container, so the far end never came up and every check
#   failed for a reason that had nothing to do with the filter.
#
# Both looked like results. Hence the precondition below.
TARGET="${KOSHER_TEST_TARGET:?set to the origin container address}"

for u in none whitelist dnsfilter filtered unfiltered; do
    useradd -M -s /usr/sbin/nologin "t-$u" 2>/dev/null
done
uid() { id -u "t-$1"; }

cat > /tmp/policy.json <<EOF
{"schema_version": 1, "revision": 1, "source": "local",
 "guardian": {"enabled": false},
 "users": [
   {"uid": $(uid none),       "username": "t-none",       "mode": "none"},
   {"uid": $(uid whitelist),  "username": "t-whitelist",  "mode": "whitelist"},
   {"uid": $(uid dnsfilter),  "username": "t-dnsfilter",  "mode": "dnsfilter"},
   {"uid": $(uid filtered),   "username": "t-filtered",   "mode": "filtered"},
   {"uid": $(uid unfiltered), "username": "t-unfiltered", "mode": "unfiltered"}
 ]}
EOF

# Before loading anything: can we reach the origin at all? A test that
# cannot tell "blocked" from "broken" reports a wall of failures either
# way, and I have now written that test twice.
if ! curl -s --max-time 5 -o /dev/null "http://$TARGET/"; then
    echo "  the origin at $TARGET is unreachable BEFORE any rules are"
    echo "  loaded, so nothing below would mean anything. Harness fault."
    exit 1
fi
say "the origin is reachable before any rules load" "ok"

kosherctl render-nft /tmp/policy.json > /tmp/kosher.nft || {
    echo "  could not render the ruleset"; exit 1; }
nft -f /tmp/kosher.nft || { echo "  the real ruleset does not LOAD"; exit 1; }
say "the rendered ruleset loads" "ok"

# Does this user reach the server? "yes" or "no".
reach() {
    if setpriv --reuid="$(uid "$1")" --regid=0 --clear-groups \
        curl -s --max-time 4 -o /dev/null "http://${2:-$TARGET}/" 2>/dev/null
    then echo yes; else echo no; fi
}

echo "reaching a server that is not loopback:"
want "no-internet is stopped"          no  "$(reach none)"
want "whitelist is stopped by default" no  "$(reach whitelist)"
want "dns-filter gets through"         yes "$(reach dnsfilter)"
want "filtered gets through"           yes "$(reach filtered)"
want "unfiltered gets through"         yes "$(reach unfiltered)"

echo "the whitelist set:"
nft add element inet kosher wl4 "{ $TARGET }"
want "a whitelisted address becomes reachable" yes "$(reach whitelist)"
nft delete element inet kosher wl4 "{ $TARGET }"
want "and unreachable again when removed"      no  "$(reach whitelist)"

echo "the captive-portal window:"
nft add element inet kosher captive "{ $(uid none) timeout 1m }"
want "an open window lets a stopped account out" yes "$(reach none)"
nft delete element inet kosher captive "{ $(uid none) }"
want "and closing it stops them again"           no  "$(reach none)"

echo "unknown users:"
# The fail-closed rule: a uid nobody configured must get nothing.
useradd -M -s /usr/sbin/nologin t-stranger 2>/dev/null
want "an account nobody configured is stopped" no \
    "$(if setpriv --reuid="$(id -u t-stranger)" --regid=0 --clear-groups \
         curl -s --max-time 4 -o /dev/null "http://$TARGET/" 2>/dev/null; \
       then echo yes; else echo no; fi)"

echo "system services:"
want "root is never filtered" yes \
    "$(curl -s --max-time 4 -o /dev/null "http://$TARGET/" && echo yes || echo no)"

echo
[ "$fail" = 0 ] && echo "all firewall checks passed" || echo "FIREWALL CHECKS FAILED"
exit "$fail"
