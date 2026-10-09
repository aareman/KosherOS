#!/usr/bin/env bash
# Developer tooling on a filtered machine (issue #34): Nix.
#
# The daemon runs as root and builds for whoever asks, so the suite checks
# the two places that could leak: who may ask (an account with no internet
# may not), and what a build may fetch (the registries, not a page).
set -uo pipefail
. "$(dirname "$0")/lib.sh"

ensure_test_users

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
    sh -c 'coredumpctl list --no-legend --since=-1h 2>/dev/null | grep -q gnome-session'
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
out=$(as nokid nix store info 2>&1)
case "$out" in
    *"not allowed to connect"*|*"not allowed"*) ok "an account with no internet is refused by the daemon" ;;
    *) bad "the no-internet account reached the daemon: $(printf '%s' "$out" | head -c 120)" ;;
esac
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

section "What a build may fetch"
# A fixed-output build runs as a nixbld user with the network. The wrong
# hash is deliberate: "hash mismatch" means the fetch SUCCEEDED (the
# firewall let it through); a connection failure means it was refused.
probe() {
    as wlkid timeout 300 nix build --impure --no-link --expr "
        with import <nixpkgs> {}; derivation {
          name = \"kosher-probe\"; system = builtins.currentSystem;
          builder = \"\${bash}/bin/bash\";
          args = [ \"-c\" \"\${curl}/bin/curl -sS --max-time 20 -o \$out $1\" ];
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
    *"curl"*|*"builder"*|*"failed"*) ok "a build cannot fetch a page (example.com refused)" ;;
    *) bad "unexpected result for example.com: $(printf '%s' "$out" | tail -3 | head -c 300)" ;;
esac

report
