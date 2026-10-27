#!/usr/bin/env bash
# Developer tooling on a filtered machine (issue #34): Nix.
#
# The daemon runs as root and builds for whoever asks, so the suite checks
# the two places that could leak: who may ask (an account with no internet
# may not), and what a build may fetch (the registries, not a page).
set -uo pipefail
. "$(dirname "$0")/lib.sh"

ensure_test_users
# This suite's "off the list" checks use example.com, and 20-enforcement
# puts example.com on wlkid's list. When the two shared a machine, the
# container and the build checks reached example.com because it WAS on
# the list. Start from an empty list, whatever ran before.
kctl set-mode "$(id -u wlkid)" whitelist >/dev/null
kctl set-whitelist "$(id -u wlkid)" >/dev/null
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

section "Without developer tools, no Nix of its own (#214)"
# The daemon refusing an account is not enough: nix builds into a store
# under ~ by itself. So the program and its libraries are root's and
# kosher-nix's, and kosherd keeps the group to the developer accounts.
# dnskid is filtered and has no developer tools.
check_contains "nix is root's and kosher-nix's alone" "root:kosher-nix 750" stat -c '%U:%G %a' /usr/bin/nix
check_contains "and so are its libraries" "root:kosher-nix 750" stat -c '%U:%G %a' /usr/lib64/libnixstorec.so.2.34.8
check_contains "the developer account is in the group" "wlkid" getent group kosher-nix
check "the account without developer tools is not" 1 sh -c 'getent group kosher-nix | grep -qw dnskid'
dhome=$(readlink -f "$(getent passwd dnskid | cut -d: -f6)")
priv="local?root=$dhome/.local/share/nix/root"
has_private() { compgen -G "$dhome/.local/share/nix/root/nix/store/*-hello-*" >/dev/null; }
check "it cannot run nix" 1 as dnskid nix --version
check "nor build into a store of its own (--store)" 1 as dnskid timeout 300 nix --store "$priv" build --no-link nixpkgs#hello
check "nor through NIX_CONFIG" 1 as dnskid env NIX_CONFIG="store = $priv" timeout 300 nix build --no-link nixpkgs#hello
check "nor through NIX_REMOTE" 1 as dnskid env NIX_REMOTE="$priv" timeout 300 nix-build --version
as dnskid mkdir -p "$dhome/.config/nix"
as dnskid sh -c "printf 'store = %s\n' '$priv' > '$dhome/.config/nix/nix.conf'"
check "nor through ~/.config/nix/nix.conf" 1 as dnskid timeout 300 nix build --no-link nixpkgs#hello
as dnskid rm -f "$dhome/.config/nix/nix.conf"
check "nor by its other names (nix-shell, nix-env)" 1 as dnskid sh -c 'nix-shell --version || nix-env --version'
check "nor through the dynamic loader" 1 as dnskid /lib64/ld-linux-x86-64.so.2 /usr/bin/nix --version
check "nor copy it" 1 as dnskid cp /usr/bin/nix "$dhome/nix"
check "nor load Nix's C API from Python" 1 as dnskid python3 -I -c 'import ctypes; ctypes.CDLL("/usr/lib64/libnixstorec.so.2.34.8")'
check "nothing reached a private store" 1 has_private
check "a developer account runs nix" 0 as wlkid nix --version
check "and root does" 0 nix --version
# The residual risk, measured rather than asserted: a Nix of the account's
# own, downloaded through its own filter (nix-portable is a GitHub release,
# and the binary cache is an ordinary site). Not a failure; see #214.
code=$(http_code_as dnskid https://cache.nixos.org/nix-cache-info)
printf '  INFO the binary cache answers dnskid with %s (a downloaded Nix would work)\n' "$code"

section "The same account keeps its Flatpaks, containers and Store (#214)"
# A Flatpak is bubblewrap in a user namespace; so is a rootless container.
# Neither may depend on Nix being open to the account.
python3 - <<'PY' >/dev/null 2>&1
import pwd
from kosherd.client import DaemonClient
client = DaemonClient()
uid = pwd.getpwnam("dnskid").pw_uid
client.set_user_can_install(uid, True)
client.install_app_for(uid, "org.gnome.Calculator")
client.install_app_for(uid, "nixpkgs#foot")
PY
for _ in $(seq 1 120); do
    flatpak info --system org.gnome.Calculator >/dev/null 2>&1 && break
    sleep 5
done
check_contains "it runs a Flatpak" "flatpak-ok" \
    as_session dnskid flatpak run --command=sh org.gnome.Calculator -c 'echo flatpak-ok'
# A Flatpak trusts the filter's certificate (#223). Inside the sandbox
# /etc is the runtime's, so the system bundle is not there; kosherd keeps a
# copy outside /etc and a global override hands it to every app. dnskid is
# made filtered for this, so its HTTPS goes through the proxy.
check "the Flatpak copy of the bundle matches the system's" 0 \
    cmp -s /var/lib/kosher-ca/tls-ca-bundle.pem /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem
check_contains "the global override shares it" "/var/lib/kosher-ca:ro" \
    cat /var/lib/flatpak/overrides/global
kctl set-mode "$(id -u dnskid)" filtered >/dev/null
wait_for_dns >/dev/null 2>&1 || true
# The mode change restarts the proxy; outside the sandbox first, so a
# refusal below is the sandbox's and not a proxy still starting.
wait_for_proxy_as dnskid
out=$(as_session dnskid flatpak run --command=sh org.gnome.Calculator -c \
    'echo | openssl s_client -connect example.com:443 -servername example.com 2>/dev/null | grep -E "^issuer=|Verify return"; curl -sS -o /dev/null -w "curl=%{http_code}\n" --max-time 20 https://example.com/' 2>&1)
case "$out" in
    *mitmproxy*"Verify return code: 0 (ok)"*curl=200*)
        ok "inside a Flatpak, a filtered account's HTTPS goes through the filter and verifies" ;;
    *) bad "inside a Flatpak the filter's certificate is not trusted: $(printf '%s' "$out" | tr '\n' ' ' | head -c 300)" ;;
esac
kctl set-mode "$(id -u dnskid)" dnsfilter >/dev/null
check_contains "it runs a rootless container" "podman-ok" \
    as_session dnskid timeout 600 podman run --rm registry.fedoraproject.org/fedora-minimal:44 echo podman-ok
for _ in $(seq 1 120); do
    test -x "$dhome/.local/state/nix/profiles/profile/bin/foot" && break
    sleep 5
done
check "the Store installs a Nix app for it (as root, into its profile)" 0 \
    test -x "$dhome/.local/state/nix/profiles/profile/bin/foot"
check_contains "which it runs" "foot version" as dnskid bash -lc "foot --version"
python3 -c 'from kosherd.client import DaemonClient; c=DaemonClient(); c.remove_app("nixpkgs#foot"); c.remove_app("org.gnome.Calculator")' >/dev/null 2>&1

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
# goes through the builders' proxy, which holds it to the strictest filter
# among the accounts with developer tools on (#199). The wrong hash is
# deliberate: "hash mismatch" means the fetch SUCCEEDED. curl fails on a
# refusal and prints the proxy's reason into the build log (with bash
# builtins: the sandbox has no cat), which is what a person building would
# read. probe ACCOUNT URL.
probe() {
    local u="$1"
    shift
    as "$u" timeout 600 nix build --impure --no-link --expr "
        with import <nixpkgs> {}; derivation {
          name = \"kosher-probe\"; system = builtins.currentSystem;
          builder = \"\${bash}/bin/bash\";
          args = [ \"-c\" \"\${curl}/bin/curl -sS --max-time 20 --fail-with-body -o \$out $1 || { while read -r l; do echo \$l; done < \$out; exit 1; }\" ];
          outputHashMode = \"flat\"; outputHashAlgo = \"sha256\";
          outputHash = \"0000000000000000000000000000000000000000000000000000000000000000\";
        }" 2>&1
}
why() { printf '%s' "$1" | tail -3 | head -c 300; }

# The only developer account is on approved sites only (wlkid), so a build
# gets the known source hosts and nothing else.
out=$(probe wlkid https://raw.githubusercontent.com/NixOS/nixpkgs/master/README.md)
case "$out" in
    *"hash mismatch"*) ok "a build fetches from a registry (GitHub)" ;;
    *) bad "a build could not fetch from GitHub: $(printf '%s' "$out" | tail -3 | head -c 300)" ;;
esac
out=$(probe wlkid https://example.com/)
case "$out" in
    *"hash mismatch"*) bad "a build fetched a page that is not a registry (example.com)" ;;
    *"not a source Nix builds may fetch from while wlkid"*)
        ok "a build cannot fetch a page (example.com refused, and the log says why)" ;;
    *) bad "unexpected result for example.com: $(printf '%s' "$out" | tail -3 | head -c 300)" ;;
esac
# An administrator can add a host, and take it away again.
check_contains "an administrator adds a build source" "example.com" \
    kctl build-sources add example.com
sleep 2   # the proxy reads its rules file again when it changes
out=$(probe wlkid https://example.com/)
case "$out" in
    *"hash mismatch"*) ok "a build fetches from a host the administrator added" ;;
    *) bad "an added host was still refused: $(printf '%s' "$out" | tail -3 | head -c 300)" ;;
esac
check_contains "and removes it" "nothing else" kctl build-sources remove example.com

# A filtered developer account with the default categories and a rule of
# its own blocking example.org.
id devkid >/dev/null 2>&1 || useradd -m devkid
kosherctl get-policy 2>/dev/null | grep -q '"devkid"' \
    || kosherctl adopt devkid filtered >/dev/null 2>&1
dev=$(id -u devkid)
kctl set-mode "$dev" filtered >/dev/null
kctl rules "$dev" clear >/dev/null
kctl rules "$dev" block example.org >/dev/null
kctl developer-tools "$dev" on >/dev/null
wait_for_dns >/dev/null 2>&1 || true
sleep 2
# Two developer accounts: the one on approved sites only is the stricter.
out=$(probe devkid https://example.com/)
case "$out" in
    *"not a source Nix builds may fetch from while wlkid"*)
        ok "with two developer accounts the stricter wins (example.com refused for wlkid)" ;;
    *) bad "with two developer accounts example.com was not refused for wlkid: $(why "$out")" ;;
esac
# The filtered one alone: exactly its filter.
kctl developer-tools "$(id -u wlkid)" off >/dev/null
sleep 2
out=$(probe devkid https://example.com/)
case "$out" in
    *"hash mismatch"*) ok "a filtered developer's build fetches what its categories allow (example.com)" ;;
    *) bad "a filtered developer's build could not fetch example.com: $(why "$out")" ;;
esac
out=$(probe devkid https://example.org/)
case "$out" in
    *"hash mismatch"*) bad "a build fetched example.org past devkid's own rule" ;;
    *"blocked by a rule on devkid's account"*)
        ok "a filtered developer's own rule reaches its builds (example.org refused)" ;;
    *) bad "unexpected result for example.org: $(why "$out")" ;;
esac
kctl developer-tools "$dev" off >/dev/null
kctl rules "$dev" clear >/dev/null
kctl developer-tools "$(id -u wlkid)" on >/dev/null

report
