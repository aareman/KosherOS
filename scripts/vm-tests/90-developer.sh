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

section "An app from nixpkgs, through the Store"
# The GitHub CLI is on the shipped approved list as nixpkgs#gh. The Store
# asks kosherd, which installs it into the asking account's own profile;
# the install is queued and reported on D-Bus signals, so wait for the
# profile to carry it rather than for the call to return.
check "nixpkgs#gh is on the approved list" 0 grep -q '"nixpkgs#gh"' /etc/kosher/catalog.json
out=$(as wlkid python3 - <<'PY' 2>&1
from kosherd.client import DaemonClient
DaemonClient().install_app("nixpkgs#gh")
print("QUEUED")
PY
)
case "$out" in
    *QUEUED*) ok "a filtered account may ask for it" ;;
    *) bad "the install was refused: $(printf '%s' "$out" | head -c 160)" ;;
esac
for _ in $(seq 1 120); do
    as wlkid python3 -c 'import sys; from kosherd.client import DaemonClient; sys.exit(0 if "nixpkgs#gh" in DaemonClient().list_installed() else 1)' 2>/dev/null && break
    sleep 5
done
check "it is installed for that account" 0 as wlkid python3 -c 'import sys; from kosherd.client import DaemonClient; sys.exit(0 if "nixpkgs#gh" in DaemonClient().list_installed() else 1)'
check_contains "and runs from a login shell" "gh version" as wlkid bash -lc 'gh --version'
check "but not for another account" 1 as dnskid python3 -c 'import sys; from kosherd.client import DaemonClient; sys.exit(0 if "nixpkgs#gh" in DaemonClient().list_installed() else 1)'
check "the Store's installed list knows who asked" 0 python3 -c 'import sys; from kosherd.client import DaemonClient; d=[x for x in DaemonClient().list_installed_details() if x["ref"]=="nixpkgs#gh"]; sys.exit(0 if d and d[0]["installed_by"]=="wlkid" else 1)'
as wlkid python3 -c 'from kosherd.client import DaemonClient; DaemonClient().remove_app("nixpkgs#gh")' >/dev/null 2>&1
for _ in $(seq 1 24); do
    as wlkid python3 -c 'import sys; from kosherd.client import DaemonClient; sys.exit(1 if "nixpkgs#gh" in DaemonClient().list_installed() else 0)' 2>/dev/null && break
    sleep 5
done
check "and can be removed again" 1 as wlkid bash -lc 'command -v gh'

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
