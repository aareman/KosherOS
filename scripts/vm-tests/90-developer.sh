#!/usr/bin/env bash
# Developer tooling on a filtered machine (issue #34): Nix.
#
# The daemon runs as root and builds for whoever asks, so the suite checks
# the two places that could leak: who may ask (an account with no internet
# may not), and what a build may fetch (the registries, not a page).
set -uo pipefail
. "$(dirname "$0")/lib.sh"

ensure_test_users
# Developer tools are a per-account switch (#123): without it the daemon
# refuses the account and its filter keeps it off the registries. The
# whitelist account is the one this suite builds as, so it gets them.
kctl developer-tools "$(id -u wlkid)" on >/dev/null
wait_for_dns >/dev/null 2>&1 || true

section "Nix is on the machine"
if ! command -v nix >/dev/null 2>&1; then
    skip "nix is not installed (needs the KosherOS image VM — just vm — not the stage-1 dev VM)"
    report
    exit $?
fi
check "the store is on /var (nix.mount)" 0 mountpoint -q /nix
check "nix-daemon.socket is active" 0 systemctl is-active --quiet nix-daemon.socket
check_contains "the daemon's socket is labelled where init may make one" "var_run_t" ls -Zd /nix/var/nix/daemon-socket
# The login screen must still come up: a session environment change for
# Nix once replaced the greeter's XDG_DATA_DIRS and GDM gave up.
check "the login screen is up (gdm active)" 0 systemctl is-active --quiet gdm
check "and no GNOME session has crashed this boot" 1 \
    sh -c 'journalctl -b --no-pager -t systemd-coredump 2>/dev/null | grep -q gnome-session'
check "devbox is in the image" 0 test -x /usr/bin/devbox
check "the devenv launcher is in the image" 0 test -x /usr/bin/devenv
check "kosherd has written the daemon's user list" 0 test -f /etc/nix/kosheros-users.conf
check_contains "the whitelist account is on it" "wlkid" cat /etc/nix/kosheros-users.conf
if grep -qw nokid /etc/nix/kosheros-users.conf; then
    bad "the no-internet account is on the daemon's user list"
else
    ok "the no-internet account is not on the daemon's user list"
fi

section "Who may use the daemon"
wait_for_dns || bad "resolver did not come back"
# The daemon closes the connection on an account it does not allow; the
# client reports that as "connection reset", and says "not allowed" only
# in the daemon's own log. The exit code is the fact.
if out=$(as nokid nix store info 2>&1); then
    bad "the no-internet account reached the daemon: $(printf '%s' "$out" | head -c 120)"
else
    ok "an account with no internet is refused by the daemon"
fi
if out=$(as wlkid nix store info 2>&1); then
    ok "a filtered account may use the daemon"
else
    bad "wlkid cannot reach the daemon: $(printf '%s' "$out" | head -c 120)"
fi

section "A filtered account can build"
# nixpkgs comes from GitHub as the account (through its own filter; the
# registries are reachable from every account), hello from the cache as
# root. The first run downloads nixpkgs: give it time.
out=$(as wlkid timeout 600 nix run nixpkgs#hello 2>&1)
case "$out" in
    *"Hello, world!"*) ok "nix run nixpkgs#hello as a whitelist account" ;;
    *) bad "nix run failed: $(printf '%s' "$out" | tail -3 | head -c 300)" ;;
esac

section "An app from nixpkgs, through the Store"
# The GitHub CLI is on the shipped approved list as nixpkgs#gh. The Store
# asks kosherd, which installs it into the asking account's own profile;
# the install is queued and reported on D-Bus signals, so wait for the
# profile to carry it rather than for the call to return.
check "nixpkgs#gh is on the approved list" 0 grep -q '"nixpkgs#gh"' /etc/kosher/catalog.json
# The Store's own call is answered only for the person at the keyboard
# (polkit: a local, active session — one with a seat, which nothing a
# harness makes has). An administrator installing FOR the account is the
# path open to root, and the one the admin app takes; the result is the
# same profile, judged by the same rule.
has_gh() { test -x "/home/$1/.local/state/nix/profiles/profile/bin/gh"; }
out=$(python3 - <<'PY' 2>&1
import pwd
from kosherd.client import DaemonClient
# An install for an account holds to its own switch (#124): it has to be
# allowed to have apps at all.
client = DaemonClient()
uid = pwd.getpwnam("wlkid").pw_uid
client.set_user_can_install(uid, True)
client.install_app_for(uid, "nixpkgs#gh")
print("QUEUED")
PY
)
queued=0
case "$out" in
    *QUEUED*) ok "an administrator may install it for a filtered account"; queued=1 ;;
    *) bad "the install was refused: $(printf '%s' "$out" | tail -2 | head -c 240)" ;;
esac
# Wait for the install only when one was queued: a refused install never
# arrives, and waiting the full ten minutes for it only made the run slow.
for _ in $(seq 1 120); do
    [ "$queued" = 1 ] || break
    has_gh wlkid && break
    sleep 5
done
check "it is installed for that account" 0 has_gh wlkid
check_contains "and runs from a login shell" "gh version" as wlkid bash -lc 'gh --version'
check "but not for another account" 1 has_gh dnskid
check "the Store's installed list knows who it is for" 0 python3 -c 'import sys; from kosherd.client import DaemonClient; d=[x for x in DaemonClient().list_installed_details() if x["ref"]=="nixpkgs#gh"]; sys.exit(0 if d and d[0]["installed_by"]=="wlkid" else 1)'
check "a no-internet account cannot have it (the daemon refuses)" 1 python3 -c 'import pwd; from kosherd.client import DaemonClient; DaemonClient().install_app_for(pwd.getpwnam("nokid").pw_uid, "nixpkgs#gh"); import time; time.sleep(20); import os, sys; sys.exit(0 if os.path.exists("/home/nokid/.local/state/nix/profiles/profile/bin/gh") else 1)'
python3 -c 'from kosherd.client import DaemonClient; DaemonClient().remove_app("nixpkgs#gh")' >/dev/null 2>&1
for _ in $(seq 1 24); do
    has_gh wlkid || break
    sleep 5
done
check "and can be removed again" 1 as wlkid bash -lc 'command -v gh'

section "Nix apps find the graphics drivers (#157)"
# A Nix build's OpenGL loader looks in /run/opengl-driver, as on NixOS.
# kosher-nix-opengl.service fetches the pinned Mesa into the store on the
# first boot with a network; wait for it rather than racing it.
for _ in $(seq 1 120); do
    systemctl is-active --quiet kosher-nix-opengl.service && break
    # A service that has already failed will not become active by waiting.
    systemctl is-failed --quiet kosher-nix-opengl.service && break
    sleep 5
done
check "the driver service has run" 0 systemctl is-active --quiet kosher-nix-opengl.service
pin=$(sed -n 's/^KOSHER_OPENGL_DRIVER=//p' /usr/share/kosher/nix-opengl-driver.env)
check_contains "/run/opengl-driver is the pinned Mesa" "$pin" readlink -f /run/opengl-driver
check "with Mesa's EGL vendor file where the loader looks" 0 \
    test -f /run/opengl-driver/share/glvnd/egl_vendor.d/50_mesa.json
# The real question: does a Nix program get a working driver? eglinfo
# from nixpkgs, without a display, as an ordinary account.
check_contains "a Nix program gets an EGL driver from it" "Mesa" \
    as wlkid bash -lc 'nix shell nixpkgs#mesa-demos -c eglinfo -B -p surfaceless 2>&1'

section "Containers are the account's"
check "docker is podman's shim" 0 test -x /usr/bin/docker
check_contains "and says so" "podman" docker --version
check "podman-compose is in the image" 0 test -x /usr/bin/podman-compose
# podman wants the account's own runtime directory (/run/user/<uid>),
# which a signed-in account has and runuser does not give: it kept root's
# and the first run failed on "mkdir /run/user/0/libpod: permission
# denied". A session (as_session) is what provides it.
pod() { as_session "$@"; }
check_contains "the whitelist account has subordinate ids" "wlkid" cat /etc/subuid
check_contains "and the firewall maps its block to its chain" "$(awk -F: '/^wlkid:/{print $2"-"($2+$3-1)}' /etc/subuid)" nft list chain inet kosher output
# A small image with curl: Fedora's own registry is a system domain.
img=registry.fedoraproject.org/fedora-minimal:44
if pod wlkid timeout 600 podman pull -q "$img" >/dev/null 2>&1; then
    ok "a whitelist account pulls from a registry"
    check "a container on a whitelist account reaches a registry" 0 \
        pod wlkid podman run --rm "$img" curl -sS --max-time 20 -o /dev/null https://registry.npmjs.org/
    check "but not a site off the list" 1 \
        pod wlkid podman run --rm "$img" curl -sS --max-time 20 -o /dev/null https://example.com/
    check "inside the container the system bundle is in place" 0 \
        pod wlkid podman run --rm "$img" grep -q BEGIN /etc/ssl/certs/ca-certificates.crt
    check_contains "and the tools inside are pointed at it" "ca-certificates.crt" \
        pod wlkid podman run --rm "$img" printenv SSL_CERT_FILE
    pod wlkid podman rmi -f "$img" >/dev/null 2>&1
else
    bad "a whitelist account could not pull $img"
fi
check "a no-internet account cannot pull" 1 pod nokid timeout 120 podman pull -q "$img"

section "What a build may fetch"
# A fixed-output build runs as a nixbld user with the network, and its web
# goes through the builders' proxy, which lets it fetch only from the known
# source hosts (#199). The wrong hash is deliberate: "hash mismatch" means
# the fetch SUCCEEDED. curl fails on a refusal and prints the proxy's
# reason into the build log, which is what a person building would read.
probe() {
    as wlkid timeout 300 nix build --impure --no-link --expr "
        with import <nixpkgs> {}; derivation {
          name = \"kosher-probe\"; system = builtins.currentSystem;
          builder = \"\${bash}/bin/bash\";
          args = [ \"-c\" \"\${curl}/bin/curl -sS --max-time 20 --fail-with-body -o \$out $1 || { cat \$out; exit 1; }\" ];
          outputHashMode = \"flat\"; outputHashAlgo = \"sha256\";
          outputHash = \"0000000000000000000000000000000000000000000000000000000000000000\";
        }" 2>&1
}
out=$(probe https://raw.githubusercontent.com/NixOS/nixpkgs/master/README.md)
case "$out" in
    *"hash mismatch"*) ok "a build fetches from a registry (GitHub)" ;;
    *) bad "a build could not fetch from GitHub: $(printf '%s' "$out" | tail -3 | head -c 300)" ;;
esac
out=$(probe https://example.com/)
case "$out" in
    *"hash mismatch"*) bad "a build fetched a page that is not a registry (example.com)" ;;
    *"not a source Nix builds may fetch from"*)
        ok "a build cannot fetch a page (example.com refused, and the log says why)" ;;
    *) bad "unexpected result for example.com: $(printf '%s' "$out" | tail -3 | head -c 300)" ;;
esac
# An administrator can add a host, and take it away again.
check_contains "an administrator adds a build source" "example.com" \
    kctl build-sources add example.com
sleep 2   # the proxy reads its rules file again when it changes
out=$(probe https://example.com/)
case "$out" in
    *"hash mismatch"*) ok "a build fetches from a host the administrator added" ;;
    *) bad "an added host was still refused: $(printf '%s' "$out" | tail -3 | head -c 300)" ;;
esac
check_contains "and removes it" "nothing else" kctl build-sources remove example.com

report
