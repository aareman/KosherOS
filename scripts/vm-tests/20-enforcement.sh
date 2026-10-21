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
# dig exits 0 whatever the answer, so the answer is what is judged: none,
# or the null address the block list gives.
ad_answer=$(dig +short +time=3 @127.0.0.1 doubleclick.net A | head -1)
if [ -z "$ad_answer" ] || [ "$ad_answer" = "0.0.0.0" ]; then
    ok "a known ad domain does not resolve"
else
    bad "a known ad domain does not resolve (got $ad_answer)"
fi

section "SSH: refused at once, and git over SSH for developer accounts (#164)"
# ssh is a server-speaks-first protocol: in the proxy it waited forever
# for a greeting that never came. Now it is refused in the mode chain, and
# an account with developer tools on may use port 22 to the git hosts
# (@git4/@git6, filled by dnsmasq as those names resolve) and nowhere else.
# Every ssh runs in batch mode with no key, so the most a reachable server
# gives is its banner and an auth denial, and that is what is judged.
if ! command -v ssh >/dev/null 2>&1; then
    skip "no ssh client in the image"
else
    id sshkid >/dev/null 2>&1 || useradd -m sshkid
    kosherctl get-policy 2>/dev/null | grep -q '"sshkid"' \
        || kosherctl adopt sshkid filtered >/dev/null 2>&1
    kctl set-mode "$(id -u sshkid)" filtered >/dev/null
    kctl developer-tools "$(id -u sshkid)" off >/dev/null
    wait_for_dns >/dev/null 2>&1 || true
    wait_for_proxy_as sshkid || true
    # ssh_as <user|-> <user@host>: prints "<seconds> <ssh's output>". "-" is root.
    ssh_as() {
        local u="$1" t0 out
        shift
        local opts=(-T -o BatchMode=yes -o StrictHostKeyChecking=no
                    -o UserKnownHostsFile=/dev/null -o ConnectTimeout=30)
        t0=$(date +%s)
        if [ "$u" = - ]; then
            out=$(timeout 40 ssh "${opts[@]}" "$@" 2>&1 </dev/null)
        else
            out=$(as "$u" timeout 40 ssh "${opts[@]}" "$@" 2>&1 </dev/null)
        fi
        printf '%s %s' "$(( $(date +%s) - t0 ))" "$(printf '%s' "$out" | tr '\n' ' ' | head -c 200)"
    }
    # An answer from the server: GitHub's greeting or its auth denial.
    answered() { printf '%s' "$1" | grep -qiE 'successfully authenticated|permission denied'; }
    # Refused, and quickly: what the firewall does, rather than a hang.
    refused_fast() {
        printf '%s' "$1" | grep -qi 'connection refused' && [ "${1%% *}" -le 5 ]
    }
    # A non-git host that runs an SSH server, to tell "refused by the
    # firewall" from "nothing listens there". Root is never filtered.
    other=test.rebex.net
    root_gh=$(ssh_as - git@github.com)
    root_other=$(ssh_as - "demo@$other")
    if answered "$root_gh"; then
        ok "root reaches GitHub over SSH (the precondition)"
    else
        skip "root cannot reach GitHub over SSH, so the git checks cannot judge: $root_gh"
    fi

    r=$(ssh_as sshkid git@github.com)
    if refused_fast "$r"; then
        ok "developer tools off: ssh git@github.com is refused at once"
    else
        bad "developer tools off: ssh git@github.com was not refused at once: $r"
    fi
    if answered "$root_other"; then
        r=$(ssh_as sshkid "demo@$other")
        if refused_fast "$r"; then
            ok "developer tools off: ssh to a non-git host is refused at once"
        else
            bad "developer tools off: ssh to a non-git host was not refused at once: $r"
        fi
    else
        skip "root cannot reach $other over SSH, so the non-git host is not judged: $root_other"
    fi

    kctl developer-tools "$(id -u sshkid)" on >/dev/null
    wait_for_dns >/dev/null 2>&1 || true
    sleep 1
    gh_ip=$(as sshkid getent ahostsv4 github.com 2>/dev/null | awk '{print $1; exit}')
    if [ -n "$gh_ip" ]; then
        check_contains "github.com's address is in the git4 set once it resolves" "$gh_ip" \
            nft list set inet kosher git4
    else
        bad "github.com did not resolve for a filtered account"
    fi
    r=$(ssh_as sshkid git@github.com)
    if answered "$r"; then
        ok "developer tools on: ssh -T git@github.com gets GitHub's answer"
    elif answered "$root_gh"; then
        bad "developer tools on: ssh -T git@github.com got no answer from GitHub: $r"
    else
        skip "developer tools on: GitHub over SSH not judged (root cannot reach it either): $r"
    fi
    if answered "$root_other"; then
        r=$(ssh_as sshkid "demo@$other")
        if refused_fast "$r"; then
            ok "developer tools on: ssh to a non-git host is still refused at once"
        else
            bad "developer tools on: ssh to a non-git host was not refused at once: $r"
        fi
    fi
    kctl developer-tools "$(id -u sshkid)" off >/dev/null
fi

section "Captive portal"
check "a temporary window can be opened" 0 kosherctl captive "$(id -u nokid)" 1
check "the window lets that user out"    0 fetch_as nokid https://example.com

report
