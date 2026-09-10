#!/usr/bin/env bash
# Is the filter actually enforcing on this boot?
#
# greenboot runs every required check after boot. If one fails on the first
# boot of a newly staged image, the boot counter runs down and the machine
# returns to the deployment it came from, by itself, with nobody present.
# That floor matters more here than on an ordinary server: a KosherOS
# machine has no sudo and no root shell, so a family cannot repair a bad
# update from the inside. The update path has to be able to undo itself.
#
# What this checks is deliberately LOCAL. Every assertion is about this
# machine's own services and its own loopback. It must never test whether
# the internet works: a family's router being off, a cable out, or an ISP
# outage would otherwise roll the operating system back, which is both
# useless and alarming. The question is "did this image bring the filter
# up", not "is the house online".
#
# Exit 0 = healthy, anything else = roll me back.
#
# And it WAITS. greenboot runs this early in boot, and the first time it
# ran it caught kosherd still starting and the resolver mid-restart (kosherd
# restarts kosher-dns on its first policy apply), called the boot red, and
# rebooted a perfectly good fresh install — twice. A check that asks "is it
# up right now" at second nine is a race, not a health check. Each
# assertion below is given up to WAIT_SECONDS to come true; a service that
# genuinely failed stays failed for all of it and is still caught. The unit
# drop-in in usr/lib/systemd/system/greenboot-healthcheck.service.d orders
# the check after the filter's units as well; the waits are the belt to
# that brace.
set -uo pipefail

WAIT_SECONDS=${KOSHER_CHECK_WAIT:-120}
fail=0
say() { printf 'kosher-filter-check: %-34s %s\n' "$1" "$2"; }

# Retry a test every two seconds until it passes or the time is up.
wait_for() {
    local deadline=$((SECONDS + WAIT_SECONDS))
    until "$@"; do
        if [ "$SECONDS" -ge "$deadline" ]; then
            return 1
        fi
        sleep 2
    done
}

require_active() {
    if wait_for systemctl is-active --quiet "$1"; then
        say "$1" "active"
    else
        say "$1" "NOT active after ${WAIT_SECONDS}s"
        fail=1
    fi
}

# The daemon: without it nothing can be administered and no policy applies.
require_active kosherd.service

# The resolver every account is forced through. If this is down, DNS is
# down for everyone, which is both a broken machine and an unfiltered one.
require_active kosher-dns.service

# The firewall is a one-shot: "active" is wrong for it, so ask whether the
# ruleset it installs is actually loaded. This is the assertion that a
# filtered account cannot reach the network directly.
nft_loaded() { nft list table inet kosher >/dev/null 2>&1; }
if wait_for nft_loaded; then
    say "nftables table inet kosher" "loaded"
else
    say "nftables table inet kosher" "MISSING after ${WAIT_SECONDS}s"
    fail=1
fi

# The resolver has to answer, not merely be running. A dnsmasq that started
# and then failed to read its config is the exact failure this catches.
resolver_answers() { timeout 5 getent hosts localhost >/dev/null 2>&1; }
if wait_for resolver_answers; then
    say "resolver answers on loopback" "yes"
else
    say "resolver answers on loopback" "NO after ${WAIT_SECONDS}s"
    fail=1
fi

# The proxy only matters if somebody on this machine is in a mode that
# needs it. Asking unconditionally would roll back a perfectly good image
# on a machine where nobody is inspected.
if kosherctl status 2>/dev/null | grep -qi 'mode: *filtered'; then
    require_active kosher-mitm.service
else
    say "kosher-mitm.service" "not needed on this machine"
fi

if [ "$fail" -ne 0 ]; then
    echo "kosher-filter-check: FAILED — this boot is not enforcing the filter" >&2
    exit 1
fi
echo "kosher-filter-check: the filter is enforcing"
