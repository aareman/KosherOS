#!/usr/bin/env bash
# Services, the D-Bus API, and the DNS plane.
set -uo pipefail
. "$(dirname "$0")/lib.sh"

# The redirect probe below runs as dnskid. Run on its own, as `just
# pr-test` runs each suite, this suite came before any other had made the
# test accounts.
ensure_test_users

section "Services"
# kosherd is started by D-Bus a moment into boot; asked straight after the
# machine answers ssh it can still be starting. Give it the time a boot
# gives it before calling it down.
for _ in $(seq 1 30); do
    systemctl is-active --quiet kosherd && break
    sleep 1
done
for s in kosher-firewall kosher-dns kosherd; do
    check "$s is active" 0 systemctl is-active --quiet "$s"
done
# The firewall must be established before any link comes up, or a machine
# is briefly unfiltered every boot.
check_contains "firewall is ordered before the network" "network-pre.target" \
    systemctl show kosher-firewall.service -p Before --value
check_contains "firewall ruleset survives reboots" "table inet kosher" \
    cat /etc/kosher/nft/kosher.nft

section "Certificates"
# The developer-tools environment names a CA bundle so curl, git, pip and
# node trust the inspect CA. Fedora 44 moved the bundle, and for one build
# every one of those tools failed every HTTPS connection. Existence is the
# whole check; the path is read from the file, not repeated here.
if [ -f /etc/profile.d/kosher-ca.sh ]; then
    ca_bundle=$(. /etc/profile.d/kosher-ca.sh 2>/dev/null; printf '%s' "${SSL_CERT_FILE:-}")
    check "the CA bundle the environment names exists ($ca_bundle)" 0 test -s "$ca_bundle"
    check "the environment.d copy names the same bundle" 0 \
        grep -q "SSL_CERT_FILE=$ca_bundle" /etc/environment.d/50-kosher-ca.conf
else
    # The stage-1 dev VM installs the stack, not the image's shell environment.
    skip "CA bundle environment (only the image ships /etc/profile.d/kosher-ca.sh)"
fi

section "Daemon API"
check "kosherctl reaches the daemon" 0 kosherctl status
check "polkit refuses an unprivileged caller" 1 as nokid kosherctl get-policy
check_contains "policy validates against the shipped schema" "revision" \
    kosherctl get-policy

section "DNS plane"
# A running systemd-resolved would be asked first by glibc (nsswitch's
# "resolve [!UNAVAIL=return]") and, given an upstream, would resolve for
# every program as a system uid — around the family resolver. Masked is
# the only state that is safe.
check_contains "systemd-resolved is masked" "masked" \
    systemctl show -p LoadState --value systemd-resolved.service
check "names resolve through the local resolver" 0 dig +time=5 +short example.com
grep -q kosher-redirect-probe /etc/hosts \
    || echo "203.0.113.77 kosher-redirect-probe.internal" >> /etc/hosts
systemctl reload kosher-dns >/dev/null 2>&1 || systemctl restart kosher-dns
# dnskid asks its own resolver (the dnsfilter accounts'), which reads the
# same hosts file.
systemctl try-restart kosher-dns-dnsfilter >/dev/null 2>&1
wait_for_dns
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
check "DoH to a public resolver IP is rejected" 1 fetch_as dnskid https://1.1.1.1

section "Tailscale (#30)"
# A real tailnet sign-in needs a person at a browser, so what is checked is
# everything up to the sign-in address: the daemon runs, the resolver is
# still ours, kosherctl answers, the sign-in is behind the guardian
# password, and Tailscale's control server resolves with a dns-filtered
# account on the machine (tailscale.com is in the proxy category, and the
# resolver used to block it for root too).
if ! command -v tailscale >/dev/null 2>&1; then
    skip "Tailscale is not installed (only the image ships it)"
else
    check "tailscaled is running" 0 systemctl is-active --quiet tailscaled
    check_contains "/etc/resolv.conf is still the local resolver" "nameserver 127.0.0.1" \
        cat /etc/resolv.conf
    check "and names no other server" 1 \
        sh -c 'grep "^nameserver" /etc/resolv.conf | grep -vq "127.0.0.1"'
    check_contains "kosherctl tailscale status answers" "not signed in" kosherctl tailscale status
    cp_answer=$(getent ahostsv4 controlplane.tailscale.com | awk '{print $1; exit}')
    if [ -n "$cp_answer" ] && [ "$cp_answer" != 0.0.0.0 ]; then
        ok "Tailscale's control server resolves with a dns-filtered account present"
    else
        bad "controlplane.tailscale.com does not resolve (got '${cp_answer:-nothing}')"
    fi
    # The guardian password, set for these checks only and removed after.
    # A wrong one and an empty one go first: a right one proves the
    # guardian for the rest of the caller's session.
    gpw=pr-test-guardian-1
    ts_sign_in() {
        timeout 120 python3 -c 'import sys; from kosherd.client import DaemonClient
print(DaemonClient().tailscale_sign_in(sys.argv[1]))' "$1" 2>&1
    }
    if python3 -c 'import sys; from kosherd.client import DaemonClient
DaemonClient().set_guardian_password("", sys.argv[1])' "$gpw" >/dev/null 2>&1; then
        out=$(ts_sign_in wrong-password)
        case "$out" in
            *https://*) bad "sign-in went ahead with a wrong guardian password: $out" ;;
            *) ok "sign-in is refused with a wrong guardian password" ;;
        esac
        out=$(ts_sign_in "")
        case "$out" in
            *https://*) bad "sign-in went ahead with no guardian password: $out" ;;
            *) ok "sign-in is refused with no guardian password" ;;
        esac
        out=$(ts_sign_in "$gpw")
        case "$out" in
            *https://login.tailscale.com/*) ok "with the guardian password, sign-in gives a login address" ;;
            *) bad "sign-in with the guardian password gave no login address: $(printf '%s' "$out" | tail -2 | head -c 240)" ;;
        esac
        check_contains "and kosherctl tailscale status shows it is waiting there" "login.tailscale.com" \
            kosherctl tailscale status
        check "sign-out needs no password" 0 kosherctl tailscale sign-out
        check_contains "and is back to not signed in" "not signed in" kosherctl tailscale status
        check_contains "resolv.conf is still the local resolver after all that" "nameserver 127.0.0.1" \
            cat /etc/resolv.conf
        check "the tailnet forward is not written while signed out" 1 \
            sh -c 'grep -qs 100.100.100.100 /etc/kosher/dnsmasq.d/tailnet.conf'
        python3 -c 'import sys; from kosherd.client import DaemonClient
DaemonClient().disable_guardian(sys.argv[1])' "$gpw" >/dev/null 2>&1
        check "the guardian password is removed again" 1 python3 -c \
            'import sys; from kosherd.client import DaemonClient; sys.exit(0 if DaemonClient().guardian_enabled() else 1)'
    else
        bad "could not set a guardian password to test the sign-in gate"
    fi
    skip "a real tailnet sign-in needs a person at the login page: names on the tailnet, the 100.100.100.100 forward, a filtered account reaching a tailnet host through its proxy"
fi

report
