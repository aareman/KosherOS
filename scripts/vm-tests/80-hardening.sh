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

# Whether a text console would give an account a shell: PAM's account
# stack for the `login` service, with PAM_TTY set to that console, which
# is the decision agetty's login makes after the password. Every getty
# signs in this way, on the screen's Ctrl+Alt+F2 to F6 and on a serial
# line. Asked of libpam directly: `login -f` on a pty of its own was tried
# first and was not reliable, because login hangs up the terminal it is
# given and the probe's end of the pty sometimes went with it.
console_admits() {
    python3 - "$1" "$2" <<'PY'
import ctypes, ctypes.util, sys
libpam = ctypes.CDLL(ctypes.util.find_library("pam") or "libpam.so.0")
class Msg(ctypes.Structure):
    _fields_ = [("msg_style", ctypes.c_int), ("msg", ctypes.c_char_p)]
class Resp(ctypes.Structure):
    _fields_ = [("resp", ctypes.c_char_p), ("resp_retcode", ctypes.c_int)]
CONV = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.POINTER(Msg)),
                        ctypes.POINTER(ctypes.POINTER(Resp)), ctypes.c_void_p)
class Conv(ctypes.Structure):
    _fields_ = [("conv", CONV), ("appdata_ptr", ctypes.c_void_p)]
user, tty = sys.argv[1], sys.argv[2]
conv = Conv(CONV(lambda n, m, r, d: 19), None)  # PAM_CONV_ERR: the account stack asks nothing
handle = ctypes.c_void_p()
libpam.pam_start.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.POINTER(Conv),
                             ctypes.POINTER(ctypes.c_void_p)]
if libpam.pam_start(b"login", user.encode(), ctypes.byref(conv), ctypes.byref(handle)):
    sys.exit(2)
libpam.pam_set_item.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_char_p]
libpam.pam_set_item(handle, 3, tty.encode())  # PAM_TTY
libpam.pam_acct_mgmt.argtypes = [ctypes.c_void_p, ctypes.c_int]
rc = libpam.pam_acct_mgmt(handle, 0)
libpam.pam_end.argtypes = [ctypes.c_void_p, ctypes.c_int]
libpam.pam_end(handle, rc)
sys.exit(0 if rc == 0 else 1)
PY
}

# Text consoles are for administrators: a supervised account is refused
# on a virtual terminal and on a serial line, and an administrator (root,
# where the machine has none yet) still gets in, which is also what shows
# the probe can tell the two apart. Closed virtual terminals would pass as
# well, but a serial line would still be open to everyone, so the login
# decision itself is what is measured.
consoles_closed() {
    local admin
    admin=$(getent group kosher-admin | cut -d: -f4 | cut -d, -f1)
    console_admits "${admin:-root}" /dev/tty2 \
        || { echo "  (the console did not admit ${admin:-root} either)"; return 1; }
    ! console_admits wlkid /dev/tty2 && ! console_admits wlkid /dev/ttyS0
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

# A real GRUB password. Fedora's stock grub.cfg contains both the word
# "superusers" and a `password_pbkdf2 root ${GRUB2_PASSWORD}` line inside
# the conditional that reads user.cfg — present whether or not a password
# was ever set, so neither word is evidence. A set password is a hash:
# GRUB2_PASSWORD=grub.pbkdf2.sha512... in user.cfg, or that literal hash
# in the config itself.
grub_password_set() {
    grep -qsE '^GRUB2_PASSWORD=grub\.pbkdf2\.' /boot/grub2/user.cfg /boot/efi/EFI/fedora/user.cfg \
        || grep -rqs 'grub\.pbkdf2\.sha512\.' /boot/grub2/grub.cfg /boot/efi/EFI/fedora/grub.cfg
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
check "the proxy is running for the filtered account" 0 systemctl is-active --quiet kosher-mitm
check "the filtered account reaches an ordinary site" 0 wait_for_proxy_as wlkid
check "the dns-filtered account reaches an ordinary site" 0 fetch_as dnskid https://example.com
# The bare-address target comes from a live lookup: a fixed one goes stale
# (example.com left 93.184.216.34 in 2025, and a probe against it "passed"
# because nothing answered).
addr=$(resolve4 example.com)
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
# Judged like F3, by the block page or a refused connection: curl exits 0
# on any answer, and the far end here (a CDN) answers a bare address with
# its own 403, which is not KosherOS letting it through or stopping it.
assert_that "[F2] dns-filtered account cannot browse by address" \
    blocked_as dnskid "http://$addr/"
assert_that "[F3] filtered account is blocked on a bare address" \
    blocked_as wlkid "http://$addr/"

# -- F4: encrypted DNS ---------------------------------------------------------------

section "[F4] Encrypted DNS"
# "By shape" means the proxy recognises a DoH request whatever the address,
# so the probe must reach the proxy: a resolver the shipped NAME lists
# already block (dns.nextdns.io answers 0.0.0.0 here) never gets there and
# would pass for the wrong reason. Precondition: the host resolves.
doh_host=cloudflare-dns.com
doh_ip=$(resolve4 "$doh_host")
if [ -z "$doh_ip" ] || [ "$doh_ip" = "0.0.0.0" ]; then
    skip "[F4] DoH blocked by shape: $doh_host is already blocked by name here, so shape cannot be told apart"
else
    assert_that "[F4] DoH to a resolver the lists do not name ($doh_host) is blocked by shape" \
        blocked_as wlkid "https://$doh_host/dns-query?dns=AAABAAABAAAAAAAAA2ZvbwA"
fi
image_only "[F4] Firefox is told never to use DoH (config)" \
    && assert_that "[F4] Firefox is told never to use DoH (config)" firefox_doh_off
image_only "[F4] Chromium is told never to use DoH (config)" \
    && check "[F4] Chromium is told never to use DoH (config)" 0 bash -c \
        'grep -rqs "DnsOverHttpsMode.*off" /etc/chromium/policies/managed /etc/opt/chrome/policies/managed'
# The Chromium the Store installs is a Flatpak, which cannot see /etc. It
# reads policies from an extension point under the system installation.
image_only "[F4] the Chromium Flatpak reads that policy" \
    && check "[F4] the Chromium Flatpak reads that policy" 0 grep -qs "DnsOverHttpsMode.*off" \
        "/var/lib/flatpak/extension/org.chromium.Chromium.Extension.system-policies/$(uname -m)/1/policies/managed/kosheros.json"

# -- F5 -------------------------------------------------------------------------------

section "[F5] Whitelist checks names, not addresses"
skip "[F5] needs the proxy's name-check mode; no deterministic probe against a shared CDN edge"

# -- F6: the LAN --------------------------------------------------------------------

section "[F6] The LAN in filtered modes"
# Measured, not grepped: the rules that refuse the LAN are not one line
# naming @lan4 (they are the mode chains' own refusal, after the local
# services are let through), so a grep for one failed while the LAN was in
# fact closed. Find a web service on the LAN that root reaches, then ask
# it the same thing as the supervised accounts. Judged by the answer, not
# by a connection: a port the proxy takes in connects to the proxy, not to
# the LAN.
gw=$(ip -4 route show default | awk '{print $3; exit}')
lan_url=""
lan_code=""
for p in 80 8080 5078 5077; do
    [ -n "$gw" ] || break
    code=$(curl -s -m 4 -o /dev/null -w '%{http_code}' "http://$gw:$p/" 2>/dev/null)
    if [ -n "$code" ] && [ "$code" != 000 ]; then
        lan_url="http://$gw:$p/"
        lan_code=$code
        break
    fi
done
lan_reached_by() {
    local out code
    out=$(runuser -u "$1" -- curl -s -m 8 -w '\n__CODE__%{http_code}' "$lan_url" 2>/dev/null)
    code=${out##*__CODE__}
    [ "$code" = "$lan_code" ] && ! printf '%s' "$out" | grep -qi "KosherOS blocked"
}
if [ -z "$lan_url" ]; then
    skip "[F6] supervised accounts are limited to local services on the LAN: no web service on the LAN answers root"
else
    check "[F6] a whitelist account cannot reach a web service on the LAN" 1 lan_reached_by wlkid
    check "[F6] a dnsfilter account cannot reach a web service on the LAN" 1 lan_reached_by dnskid
fi

# -- F7: the boot path ----------------------------------------------------------------

section "[F7] The boot path"
image_only "[F7] the boot menu is password-protected" \
    && assert_that "[F7] the boot menu is password-protected" grub_password_set
# Encryption is the installer's job (#117). A development disk, made from
# the image with a root ssh key in it, is never encrypted, so there the
# check has nothing to say.
if [ -s /root/.ssh/authorized_keys ]; then
    skip "[F7] the disk is encrypted: a development disk is never encrypted; the installer's job (#117)"
else
    image_only "[F7] the disk is encrypted" \
        && assert_that "[F7] the disk is encrypted" bash -c 'lsblk -rno TYPE | grep -q crypt'
fi
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
# The policy is per registry (#70): KosherOS's own repository needs the
# KosherOS key's signature, the public registries developers pull from
# are taken as they are (rootless podman reads the same policy), and an
# unknown registry is refused. So "insecureAcceptAnything" legitimately
# stays in the file; what matters is the default and the KosherOS entry.
policy_default() {
    python3 -c 'import json; print(json.load(open("/etc/containers/policy.json"))["default"][0]["type"])'
}
policy_kosheros() {
    python3 -c 'import json; k = json.load(open("/etc/containers/policy.json"))["transports"]["docker"]["ghcr.io/aareman/kosher-linux"][0]; print(k["type"], k.get("keyPath", ""))'
}
booted_signature() {
    bootc status --format json | python3 -c 'import json, sys; i = json.load(sys.stdin)["status"]["booted"]["image"]["image"]; print(i["image"], i.get("signature"))'
}
# The policy, the key and registries.d ship in the image; the stage-1 VM
# installs the stack and nothing of that, so these say so rather than fail.
if image_only "[F10] the image-trust policy (stage-1 VM has none)"; then
    check_contains "[F10] an image from an unknown registry is refused" "reject" policy_default
    check_contains "[F10] a KosherOS image needs the KosherOS key's signature" \
        "sigstoreSigned /etc/pki/containers/kosheros-images.pub" policy_kosheros
    check "[F10] the KosherOS public key is on the machine" 0 test -s /etc/pki/containers/kosheros-images.pub
    check "[F10] the signatures are looked for where cosign puts them" 0 \
        grep -q 'use-sigstore-attachments: true' /etc/containers/registries.d/kosheros.yaml
fi
# A developer's rootless pull from a public registry still works: the
# whole point of the per-registry policy. The registries are open to an
# account with developer tools (#123), and rootless podman needs the
# account's own session for its runtime directory.
kctl developer-tools "$(id -u wlkid)" on >/dev/null
wait_for_dns >/dev/null 2>&1 || true
check "[F10] a rootless pull from Docker Hub is still allowed" 0 \
    as_session wlkid podman pull -q docker.io/library/hello-world
# Off again: the checks below (N2) hold the whitelist account to its list.
kctl developer-tools "$(id -u wlkid)" off >/dev/null
wait_for_dns >/dev/null 2>&1 || true
# bootc verifies only when the deployment says so. A machine installed
# from the ISO starts unverified; kosherd's first update switches it with
# --enforce-container-sigpolicy. A machine running its own build (a test
# VM on localhost/...) is held to nothing by the policy: nothing to see.
case "$(booted_signature)" in
    ghcr.io/aareman/kosher-linux*)
        check_contains "[F10] bootc enforces the policy on this deployment" "containerPolicy" booted_signature ;;
    *) skip "[F10] bootc enforcement: this machine does not run a KosherOS release image" ;;
esac

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

# -- N: since the September review (developer tooling, page filter, trust) --------
# Numbered N1–N9 in the October comment on #33. Nix is only on the image,
# so the Nix lines skip on the stage-1 dev VM and say so.

section "[N] Since the September review"
nix_here() { command -v nix >/dev/null 2>&1; }

if nix_here; then
    check "[N1] a supervised account is not a nix-daemon user" 1 \
        as wlkid timeout 30 nix store ping
    # A real install, because that is what the finding is about; removed
    # again afterwards. Today it succeeds: every account with internet is
    # an allowed user and nothing consults the Store's list.
    check "[N3] a supervised account cannot install an unapproved nixpkgs package from a shell" 1 \
        as wlkid timeout 600 nix profile install nixpkgs#hello
    as wlkid timeout 120 nix profile remove hello >/dev/null 2>&1 || true
else
    skip "[N1] nix-daemon users (nix is only on the image VM)"
    skip "[N3] shell install of an unapproved nixpkgs package (nix is only on the image VM)"
fi
check "[N1] build users cannot reach loopback services (config)" 0 \
    bash -c 'nft list chain inet kosher output | grep -qE "skgid .*oif \"lo\" .*reject"'
# The builders used to get the whole system address set, CDN edges and
# all; now their web goes through the proxy and the chain grants no set.
check "[N1] the builders get no address set of their own (config)" 1 \
    bash -c 'nft list chain inet kosher nix_build 2>/dev/null | grep -q "@sys4"'
check "[N1] the builders' web is redirected into the proxy (config)" 0 \
    bash -c 'nft list chain inet kosher dns_redirect | grep -qE "skgid .*tcp dport \{ 80, 443 \} redirect"'
# N2 is measured below, once wlkid is back in whitelist mode.
image_only "[N6] no development registry in the image policy" \
    && check "[N6] no development registry in the image policy" 1 \
        grep -q "10.0.2.2" /etc/containers/policy.json
# Any page can POST to the judge endpoint today; after the fix it needs
# the token the injected script was given.
judge_code=$(runuser -u wlkid -- curl -sS -o /dev/null -w '%{http_code}' --max-time 20 \
    -X POST -H 'content-type: application/json' -d '{"url":"https://example.com/","text":"probe"}' \
    https://example.com/__kosheros__/judge 2>/dev/null || echo 000)
assert_that "[N7] the judge endpoint refuses a POST without the page token (got $judge_code)" \
    test "$judge_code" = "403"
words_code=$(runuser -u wlkid -- curl -sS -o /dev/null -w '%{http_code}' --max-time 20 \
    https://example.com/__kosheros__/words.json 2>/dev/null || echo 000)
assert_that "[N7] the word list is not served without the page token (got $words_code)" \
    test "$words_code" = "403"
# The plain resolver answers unfiltered accounts; a dns-filtered one must
# not be able to ask it directly, as loopback is never redirected.
with_unfiltered_account
check "[N8] the plain resolver refuses a dns-filtered account" 1 \
    dns_query_as dnskid 127.0.0.1 5354 example.com
without_unfiltered_account

# -- restore the fixture the other suites expect ------------------------------------------------

section "Restoring the fixture"
kctl rules "$wl" clear >/dev/null
check "wlkid is back in whitelist mode" 0 \
    kosherctl set-mode "$wl" whitelist --guardian-password ""
wait_for_dns
sleep 2
# The developer registries joined the system set every mode reaches on
# 80/443 without the proxy, so "whitelist only" stopped meaning the
# family's list. GitHub alone hosts anything.
check "[N2] a whitelist-only account cannot reach github.com directly" 1 \
    fetch_as wlkid https://github.com/

report
