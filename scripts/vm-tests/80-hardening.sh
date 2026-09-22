#!/usr/bin/env bash
# Hardening: the protections the red-team review on issue #33 asked for,
# asserted on a live machine from a supervised account.
#
# Most of these FAIL today. That is the point: the run shows the gaps on a
# real machine, and the same suite is the acceptance test as each fix lands.
# Every description carries its finding number from
# https://github.com/aareman/KosherOS/issues/33 so a line maps straight back
# to the report. "(config)" marks a check that reads configuration because
# a live probe would be ambiguous (a refused connection looks the same from
# the firewall and from a closed port).
set -uo pipefail
. "$(dirname "$0")/lib.sh"

# -- helpers ------------------------------------------------------------------

# A fetch counts as blocked when the connection fails outright or the
# proxy's own block page comes back. A real page from the far end — any
# status, 404 included — means the request was relayed, and is not blocked.
blocked_as() {
    local u="$1" url="$2" out code
    out=$(runuser -u "$u" -- curl -sS --max-time 20 -w '\n__CODE__%{http_code}' "$url" 2>/dev/null)
    code=${out##*__CODE__}
    [ -n "$out" ] || code=000
    [ "$code" = "000" ] && return 0
    printf '%s' "$out" | grep -qi "KosherOS blocked"
}

# Did the real SSH server answer? GitHub's SSH layer says "Permission denied
# (publickey)" to a key it does not know; seeing that line means the stream
# reached the far end unread. Exit codes cannot tell that apart from "refused
# by the proxy" (ssh exits 255 either way), hence the text.
reaches_ssh_as() {
    local u="$1" host="$2" out
    # Captured rather than piped: with pipefail on, a grep -q that closes
    # the pipe early turns a match into a failing pipeline. It did.
    out=$(runuser -u "$u" -- timeout 15 ssh -o BatchMode=yes -o StrictHostKeyChecking=no \
        -o UserKnownHostsFile=/dev/null -o ConnectTimeout=8 "git@$host" 2>&1)
    printf '%s' "$out" | grep -q "Permission denied"
}

# A server the same account starts on loopback must stay reachable: sending
# every port through the proxy must never catch 127.0.0.1.
local_server_as() {
    local u="$1" port="$2" pid ok=1
    runuser -u "$u" -- python3 -m http.server --bind 127.0.0.1 "$port" \
        --directory /tmp >/dev/null 2>&1 &
    pid=$!
    sleep 1
    fetch_as "$u" "http://127.0.0.1:$port/" && ok=0
    kill "$pid" 2>/dev/null
    pkill -u "$u" -f "http.server --bind 127.0.0.1 $port" 2>/dev/null
    wait "$pid" 2>/dev/null
    return $ok
}

# One STUN Binding request to a public server; an answer means UDP to a high
# port got out, which is what a video call needs.
stun_answers_as() {
    runuser -u "$1" -- python3 - <<'PY'
import os, socket, sys
req = b"\x00\x01\x00\x00\x21\x12\xa4\x42" + os.urandom(12)
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.settimeout(5)
try:
    s.sendto(req, ("stun.l.google.com", 19302))
    data, _ = s.recvfrom(1024)
    sys.exit(0 if data[:2] == b"\x01\x01" else 1)
except OSError:
    sys.exit(1)
PY
}

consoles_closed() {
    [ "$(systemctl show -p LoadState --value getty@tty2.service 2>/dev/null)" = masked ] && return 0
    systemd-analyze cat-config systemd/logind.conf 2>/dev/null | grep -qE '^\s*NAutoVTs\s*=\s*0'
}

firefox_doh_off() {
    python3 - <<'PY'
import json, sys
try:
    doc = json.load(open("/etc/firefox/policies/policies.json"))
    doh = doc.get("policies", doc).get("DNSOverHTTPS", {})
except (OSError, ValueError):
    sys.exit(1)
sys.exit(0 if doh.get("Enabled") is False and doh.get("Locked") is True else 1)
PY
}

# The stage-1 dev VM is Fedora Cloud with the stack installed on top; only
# the bootc image carries the boot, console, browser-policy and image-trust
# configuration. Those checks are skipped there rather than failing for a
# reason that has nothing to do with the finding.
. /etc/os-release
on_image() { [ "${VARIANT_ID:-}" = "kosheros" ]; }
image_only() {
    on_image && return 0
    skip "$1 (needs the KosherOS image VM — just vm — not the stage-1 dev VM)"
    return 1
}

# A real GRUB password, not the word "superusers": Fedora's stock grub.cfg
# carries that word inside the conditional that reads user.cfg, whether or
# not a password was ever set.
grub_password_set() {
    grep -qs 'GRUB2_PASSWORD=' /boot/grub2/user.cfg /boot/efi/EFI/fedora/user.cfg \
        || grep -rqs 'password_pbkdf2' /boot/grub2/grub.cfg /boot/efi/EFI/fedora/grub.cfg
}

# -- preconditions ------------------------------------------------------------
# A failure here is about the run, not the machine: nothing below means
# anything unless the stack is up and the filtered account can browse.

section "Preconditions"
for s in kosher-firewall kosher-dns kosherd; do
    check "$s is active" 0 systemctl is-active --quiet "$s"
done
check "the firewall table is loaded" 0 nft list table inet kosher
check "the internet is reachable at all (root)" 0 fetch https://example.com

ensure_test_users
wl=$(id -u wlkid)
dk=$(id -u dnskid)
kctl set-mode "$wl" filtered >/dev/null
kctl set-mode "$dk" dnsfilter >/dev/null
kctl rules "$wl" clear >/dev/null
# portquiz.net answers plain HTTP on every port, which makes it the one
# deterministic probe for "web on a non-standard port".
kctl rules "$wl" block "portquiz.net*" >/dev/null
wait_for_dns  # every policy change restarts the resolver
sleep 2       # ...and the proxy needs a moment to bind
check "the proxy is running for the filtered account" 0 systemctl is-active --quiet kosher-mitm
check "the filtered account reaches an ordinary site" 0 fetch_as wlkid https://example.com
check "the dns-filtered account reaches an ordinary site" 0 fetch_as dnskid https://example.com
# The bare-address target comes from a live lookup: a fixed one goes stale
# (example.com left 93.184.216.34 in 2025, and a probe against it "passed"
# because nothing answered).
addr=$(dig +short +time=3 example.com A | grep -E '^[0-9.]+$' | head -1)
check "a bare-address probe target answers (root, http://$addr/)" 0 fetch "http://$addr/"

# -- F1: every port goes through the proxy ---------------------------------------

section "[F1] Every port goes through the proxy"
assert_that "[F1] web on a non-standard port is filtered (rule-blocked portquiz.net:8080)" \
    blocked_as wlkid "http://portquiz.net:8080/"
check "[F1] a non-HTTP protocol is refused, not relayed (ssh to github.com)" 1 \
    reaches_ssh_as wlkid github.com
assert_that "[F1] loopback is never redirected (own server on 127.0.0.1:8081)" \
    local_server_as wlkid 8081
assert_that "[F1] video calls work by default (STUN over UDP 19302)" \
    stun_answers_as wlkid
# Refine once the rule exists and has a shape: today the chain is a jump to
# evasion_block and an accept, so any udp rule at all is the assertion.
check_contains "[F1] UDP below 1024 is rejected for filtered accounts (config)" "udp dport" \
    nft list chain inet kosher mode_filtered

# -- F2, F3: browsing by address instead of name ----------------------------------
# Plain HTTP on purpose: over HTTPS the far end's certificate does not name
# the address, so the proxy's upstream check would fail for a reason that
# has nothing to do with the rule under test.

section "[F2] [F3] Browsing by address instead of name"
check "[F2] dns-filtered account cannot browse by address" 1 \
    fetch_as dnskid "http://$addr/"
assert_that "[F3] filtered account is blocked on a bare address" \
    blocked_as wlkid "http://$addr/"

# -- F4: encrypted DNS ---------------------------------------------------------------

section "[F4] Encrypted DNS"
# NextDNS is not in the shipped address list, so this passes only when the
# proxy recognises a DoH request by its shape.
assert_that "[F4] DoH to a resolver not on the address list is blocked by shape" \
    blocked_as wlkid "https://dns.nextdns.io/dns-query?dns=AAABAAABAAAAAAAAA2ZvbwA"
image_only "[F4] Firefox is told never to use DoH (config)" \
    && assert_that "[F4] Firefox is told never to use DoH (config)" firefox_doh_off
image_only "[F4] Chromium is told never to use DoH (config)" \
    && check "[F4] Chromium is told never to use DoH (config)" 0 bash -c \
        'grep -rqs "DnsOverHttpsMode.*off" /etc/chromium/policies/managed /etc/opt/chrome/policies/managed'

# -- F5 -------------------------------------------------------------------------------

section "[F5] Whitelist checks names, not addresses"
skip "[F5] needs the proxy's name-check mode; no deterministic probe against a shared CDN edge"

# -- F6: the LAN --------------------------------------------------------------------

section "[F6] The LAN in filtered modes"
check_contains "[F6] filtered mode limits the LAN to local services (config)" "local_services" \
    nft list chain inet kosher mode_filtered
check_contains "[F6] dns-filtered mode limits the LAN to local services (config)" "local_services" \
    nft list chain inet kosher mode_dnsfilter

# -- F7: the boot path ----------------------------------------------------------------

section "[F7] The boot path"
image_only "[F7] the boot menu is password-protected" \
    && assert_that "[F7] the boot menu is password-protected" grub_password_set
image_only "[F7] the disk is encrypted" \
    && assert_that "[F7] the disk is encrypted" bash -c 'lsblk -rno TYPE | grep -q crypt'
image_only "[F7] no dracut shell on a failed boot" \
    && check_contains "[F7] no dracut shell on a failed boot" "rd.shell=0" cat /proc/cmdline

# -- F8: text consoles ----------------------------------------------------------------

section "[F8] Text consoles"
image_only "[F8] text consoles are not offered to accounts" \
    && assert_that "[F8] text consoles are not offered to accounts" consoles_closed

# -- F9 -------------------------------------------------------------------------------

section "[F9] App list wording"
skip "[F9] UI wording; not machine-checkable"

# -- F10: image trust ------------------------------------------------------------------

section "[F10] Pulled images are verified"
image_only "[F10] the image policy accepts nothing unsigned" \
    && check "[F10] the image policy accepts nothing unsigned" 1 \
        grep -q insecureAcceptAnything /etc/containers/policy.json
image_only "[F10] the image policy requires a sigstore signature" \
    && check_contains "[F10] the image policy requires a sigstore signature" "sigstoreSigned" \
        cat /etc/containers/policy.json

# -- F11: rollback ---------------------------------------------------------------------

section "[F11] Rollback"
check "[F11] manual rollback needs the guardian password" 0 python3 -c \
    'from kosherd.access import GUARDIAN_GATED as g; import sys; sys.exit(0 if "Rollback" in g else 1)'

# -- F12: unknown protocols on 443 ---------------------------------------------------------

section "[F12] Unknown protocols on 443"
check_contains "[F12] the proxy refuses rather than relays what is not HTTP" "rawtcp=false" \
    systemctl cat kosher-mitm

# -- F13: guardian lockout ------------------------------------------------------------------

section "[F13] Guardian lockout"
check "[F13] the lockout survives a reboot (state under /var/lib)" 0 python3 -c \
    'from kosherd import guardian; import sys; sys.exit(0 if str(guardian.STATE_PATH).startswith("/var/lib") else 1)'

# -- F14 --------------------------------------------------------------------------------------

section "[F14] Captive window scope"
skip "[F14] decision still open on #33"

# -- restore the fixture the other suites expect ------------------------------------------------

section "Restoring the fixture"
kctl rules "$wl" clear >/dev/null
check "wlkid is back in whitelist mode" 0 \
    kosherctl set-mode "$wl" whitelist --guardian-password ""

report
