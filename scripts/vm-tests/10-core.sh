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
    # Tailscale's own sites and the tailnet's names (#211): blocked while
    # nothing is set up, open for every account once a sign-in has started,
    # blocked again after the sign-out. nokid is made a filtered account
    # blocking "proxy" for this and put back after; wlkid is whitelist and
    # dnskid dnsfilter. The page is the sign-in page a person opens.
    no_uid=$(id -u nokid)
    no_cats=$(kosherctl get-policy | python3 -c 'import json, sys
u = [u for u in json.load(sys.stdin)["users"] if u["username"] == "nokid"][0]
print(" ".join(u.get("blocked_categories", [])))')
    kctl set-mode "$no_uid" filtered >/dev/null
    kctl categories "$no_uid" block proxy >/dev/null
    wait_for_dns
    sleep 2
    ts_reach() {
        local page="/tmp/ts-reach-$1.html" code
        rm -f "$page"
        code=$(http_code_as "$1" https://login.tailscale.com/ "$page")
        if [ "$code" = 000 ]; then echo unreachable
        elif grep -qi "Blocked — KosherOS" "$page" 2>/dev/null; then echo blocked
        else echo "open ($code)"
        fi
    }
    if python3 -c 'from kosherd import categories as c; import sys
sys.exit(0 if "proxy" in c.load_any().categories_of("login.tailscale.com") else 1)' 2>/dev/null; then
        got=$(ts_reach nokid)
        assert_that "signed out, a filtered account gets the block page for login.tailscale.com (got $got)" \
            test "$got" = blocked
    else
        skip "login.tailscale.com is not in the proxy category on this machine's catalogue"
    fi
    got=$(ts_reach wlkid)
    assert_that "signed out, a whitelist account cannot reach login.tailscale.com (got $got)" \
        test "$got" = unreachable
    check "and the proxy is told of no Tailscale names" 1 \
        grep -q '"\*\.tailscale\.com"' /var/lib/kosher-mitm/rules.json
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
        # The sign-in returns once the names are in force; the apply
        # restarted the resolvers and the proxy.
        wait_for_dns
        sleep 2
        check_contains "with a sign-in waiting, the proxy is told Tailscale's names" '"*.tailscale.com"' \
            cat /var/lib/kosher-mitm/rules.json
        check_contains "and they are in the system set" "nftset=/tailscale.com/" \
            cat /etc/kosher/dnsmasq.d/whitelist.conf
        check "but no tailnet, and never all of ts.net, before a sign-in finishes" 1 \
            grep -qE "[/.]ts\\.net/" /etc/kosher/dnsmasq.d/whitelist.conf
        for who in nokid wlkid dnskid; do
            got=$(ts_reach "$who")
            case "$got" in
                open*) ok "with a sign-in waiting, $who reaches login.tailscale.com ($got)" ;;
                *) bad "with a sign-in waiting, $who could not reach login.tailscale.com ($got)" ;;
            esac
        done
        check "sign-out needs no password" 0 kosherctl tailscale sign-out
        check_contains "and is back to not signed in" "not signed in" kosherctl tailscale status
        check_contains "resolv.conf is still the local resolver after all that" "nameserver 127.0.0.1" \
            cat /etc/resolv.conf
        check "the tailnet forward is not written while signed out" 1 \
            sh -c 'grep -qs 100.100.100.100 /etc/kosher/dnsmasq.d/tailnet.conf'
        wait_for_dns
        sleep 2
        check "after the sign-out the proxy is told of no Tailscale names" 1 \
            grep -q '"\*\.tailscale\.com"' /var/lib/kosher-mitm/rules.json
        check "and they are out of the system set" 1 \
            grep -q "nftset=/tailscale.com/" /etc/kosher/dnsmasq.d/whitelist.conf
        if grep -q '"\*\.tailscale\.com"' /var/lib/kosher-mitm/rules.json; then
            # What kosherd saw, for whoever reads the failure.
            echo "    status after the sign-out: $(kosherctl tailscale status --json 2>&1 | tr -d '\n' | head -c 600)"
            journalctl -u kosherd -b --no-pager 2>/dev/null | grep -i tailscale | tail -12 | sed 's/^/    /'
        fi
        got=$(ts_reach wlkid)
        assert_that "after the sign-out a whitelist account cannot reach login.tailscale.com (got $got)" \
            test "$got" = unreachable
        if python3 -c 'from kosherd import categories as c; import sys
sys.exit(0 if "proxy" in c.load_any().categories_of("login.tailscale.com") else 1)' 2>/dev/null; then
            got=$(ts_reach nokid)
            assert_that "and a filtered account gets the block page again (got $got)" \
                test "$got" = blocked
        fi
        python3 -c 'import sys; from kosherd.client import DaemonClient
DaemonClient().disable_guardian(sys.argv[1])' "$gpw" >/dev/null 2>&1
        check "the guardian password is removed again" 1 python3 -c \
            'import sys; from kosherd.client import DaemonClient; sys.exit(0 if DaemonClient().guardian_enabled() else 1)'
    else
        bad "could not set a guardian password to test the sign-in gate"
    fi
    # nokid back to what it was.
    kctl categories "$no_uid" clear >/dev/null
    read -ra no_cat_list <<< "$no_cats"
    [ "${#no_cat_list[@]}" -gt 0 ] && kctl categories "$no_uid" block "${no_cat_list[@]}" >/dev/null
    kctl set-mode "$no_uid" none >/dev/null
    check_contains "nokid is back to no internet" "none" \
        sh -c "kosherctl get-policy | python3 -c 'import json, sys; print([u[\"mode\"] for u in json.load(sys.stdin)[\"users\"] if u[\"username\"] == \"nokid\"][0])'"
    skip "a real tailnet sign-in needs a person at the login page: names on the tailnet, the 100.100.100.100 forward, a filtered account reaching a tailnet host through its proxy"
fi

section "Automatic updates (#19)"
timer=kosher-auto-update.timer
schedule() { kosherctl get-policy | python3 -c 'import json,sys; print((json.load(sys.stdin).get("updates") or {}).get("auto", "weekly"))'; }
check "the timer is enabled" 0 systemctl is-enabled --quiet "$timer"
check "and running, on the default weekly schedule" 0 systemctl is-active --quiet "$timer"
check_contains "the policy says weekly" "weekly" schedule
check_contains "the timer has a next run" "$timer" systemctl list-timers --all --no-legend "$timer"
check_contains "Fedora's own timer, which restarts the computer, stays off" "disabled" \
    systemctl is-enabled bootc-fetch-apply-updates.timer
kosherctl updates auto off >/dev/null
check "off stops the timer" 1 systemctl is-active --quiet "$timer"
check_contains "and the run does not check at all" "automatic updates are off" \
    kosherctl updates run --now
kosherctl updates auto biweekly >/dev/null
check "every 2 weeks starts it again" 0 systemctl is-active --quiet "$timer"
check_contains "and the policy says so" "biweekly" schedule
kosherctl updates notify off >/dev/null
check_contains "the notice can be turned off" "notice when ready: off" kosherctl updates
kosherctl updates notify on >/dev/null
kosherctl updates auto weekly >/dev/null
check_contains "kosherd answers any account about a waiting update without bootc" "{" \
    python3 -c 'from kosherd.client import DaemonClient; import json; print(json.dumps(DaemonClient().pending_update()))'

# Staging, without a restart. The harness's registry (10.0.2.2) is trusted
# again for this alone, and the image this machine runs is pushed to its
# own tag once more with one label added: a new image with the same
# layers, which is what a new build looks like to bootc, and a download of
# almost nothing. The run removes this machine's image tag when it ends,
# and no other machine checks it again. The automatic run must find it,
# download it, leave it staged, not restart, and announce it once.
relabel() {
    python3 - "$1" <<'PY'
import hashlib, json, sys, time, urllib.request
ref = sys.argv[1]
host, rest = ref.split("/", 1)
repo, tag = rest.rsplit(":", 1)
base = f"http://{host}/v2/{repo}"
kinds = ("application/vnd.oci.image.manifest.v1+json,"
         "application/vnd.docker.distribution.manifest.v2+json")
with urllib.request.urlopen(urllib.request.Request(f"{base}/manifests/{tag}",
                                                   headers={"Accept": kinds})) as r:
    kind = r.headers["Content-Type"].split(";")[0]
    manifest = json.load(r)
if "config" not in manifest:
    sys.exit("not a single-image manifest")
with urllib.request.urlopen(f"{base}/blobs/{manifest['config']['digest']}") as r:
    config = json.load(r)
config.setdefault("config", {}).setdefault("Labels", {})["org.kosherlinux.test.auto19"] = str(time.time())
blob = json.dumps(config).encode()
digest = "sha256:" + hashlib.sha256(blob).hexdigest()
start = urllib.request.urlopen(urllib.request.Request(f"{base}/blobs/uploads/", method="POST"))
where = start.headers["Location"]
if where.startswith("/"):
    where = f"http://{host}{where}"
where += ("&" if "?" in where else "?") + "digest=" + digest
urllib.request.urlopen(urllib.request.Request(where, data=blob, method="PUT",
                       headers={"Content-Type": "application/octet-stream"}))
manifest["config"]["digest"], manifest["config"]["size"] = digest, len(blob)
body = json.dumps(manifest).encode()
urllib.request.urlopen(urllib.request.Request(f"{base}/manifests/{tag}", data=body, method="PUT",
                       headers={"Content-Type": kind}))
print("sha256:" + hashlib.sha256(body).hexdigest())
PY
}
booted_ref=$(bootc status --format json 2>/dev/null | python3 -c 'import json,sys; print(json.load(sys.stdin)["status"]["booted"]["image"]["image"]["image"])' 2>/dev/null)
case "$booted_ref" in
10.0.2.2:*/*:*)
    registry=${booted_ref%%/*}
    cp -a /etc/containers/policy.json /run/pr-test-policy.json
    restore_trust() {
        cp -a /run/pr-test-policy.json /etc/containers/policy.json
        rm -f /etc/containers/registries.conf.d/50-pr-test-auto.conf
    }
    trap restore_trust EXIT
    mkdir -p /etc/containers/registries.conf.d
    printf '[[registry]]\nlocation = "%s"\ninsecure = true\n' "$registry" \
        > /etc/containers/registries.conf.d/50-pr-test-auto.conf
    python3 -c 'import json, sys; p = "/etc/containers/policy.json"; d = json.load(open(p)); d.setdefault("transports", {}).setdefault("docker", {})[sys.argv[1]] = [{"type": "insecureAcceptAnything"}]; json.dump(d, open(p, "w"), indent=4)' "$registry"
    newer=$(relabel "$booted_ref" 2>/run/pr-test-relabel.err)
    if [ -n "$newer" ]; then
        ok "the running image is pushed again with a label added ($newer)"
        boot_before=$(cat /proc/sys/kernel/random/boot_id)
        rm -f /run/pr-test-update-ready.log
        busctl --system monitor --match \
            "type='signal',interface='org.kosherlinux.Daemon1.System',member='UpdateReady'" \
            > /run/pr-test-update-ready.log 2>&1 &
        monitor=$!
        sleep 1
        run=$(kosherctl updates run --now 2>&1)
        if printf '%s' "$run" | grep -q downloading; then
            ok "the automatic run finds it and starts downloading"
            staged=""
            for _ in $(seq 1 200); do
                staged=$(bootc status --format json 2>/dev/null | python3 -c 'import json,sys; s = json.load(sys.stdin)["status"].get("staged") or {}; print((s.get("image") or {}).get("imageDigest") or "")' 2>/dev/null)
                [ "$staged" = "$newer" ] && grep -q "$newer" /run/pr-test-update-ready.log && break
                kosherctl updates 2>/dev/null | grep -q 'last try failed' && break
                sleep 3
            done
            if [ "$staged" = "$newer" ]; then
                ok "it is staged for the next restart"
            else
                bad "it is not staged (staged: ${staged:-nothing}); kosherd: $(journalctl -u kosherd --no-pager -o cat -n 400 | grep -i -E 'automatic|update' | tail -6 | tr '\n' ' ' | head -c 900)"
            fi
            assert_that "and the computer did not restart" \
                test "$(cat /proc/sys/kernel/random/boot_id)" = "$boot_before"
            check_contains "kosherctl says it waits for a restart" "waiting for a restart" kosherctl updates
            check_contains "any account can see it waits" "$newer" \
                python3 -c 'from kosherd.client import DaemonClient; print(DaemonClient().pending_update())'
            # A second run finds the same download and must not announce again.
            check_contains "a second run sees it is already downloaded" "already downloaded" \
                kosherctl updates run --now
            sleep 3
            announced=$(grep -c 'BOOLEAN true' /run/pr-test-update-ready.log)
            assert_that "the notice was sent once ($announced)" test "$announced" = 1
            # Put the machine back: the staged copy is discarded.
            out=$(ostree admin undeploy 0 2>&1)
            if [ ! -e /run/ostree/staged-deployment ]; then
                ok "the staged copy is discarded again"
            else
                bad "could not discard the staged copy: $out"
            fi
            check_contains "and once it is gone, nothing waits" "{}" \
                python3 -c 'from kosherd.client import DaemonClient; import json; print(json.dumps(DaemonClient().pending_update()))'
        else
            bad "the automatic run did not start a download: $run; kosherd: $(journalctl -u kosherd --no-pager -o cat -n 400 | grep -i -E 'automatic|update' | tail -5 | tr '\n' ' ' | head -c 900)"
        fi
        kill "$monitor" 2>/dev/null
    else
        skip "could not push the running image again with a label added: $(head -c 300 /run/pr-test-relabel.err)"
    fi
    restore_trust
    trap - EXIT
    ;;
*)
    skip "this machine does not run an image from the harness registry ($booted_ref): staging not checked"
    ;;
esac

report
