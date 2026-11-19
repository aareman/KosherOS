#!/usr/bin/env bash
# The approved-app allowlist: what may be installed, by whom, and who may run it.
#
# kosherctl deliberately does not wrap every Apps method (the Store is the
# UI for those), so this suite drives the daemon through its client.
set -uo pipefail
. "$(dirname "$0")/lib.sh"

ensure_test_users
uid=$(id -u wlkid)
arch=$(flatpak --default-arch 2>/dev/null || echo x86_64)

section "Allowlist"
# Asked for an approved-only account that may install. Root's own view is
# an administrator's, which is open to the whole store, so an install as
# root is not a test of the list.
refusal=$(python3 - "$uid" <<'PY' 2>&1
import sys
from kosherd.client import DaemonClient
uid = int(sys.argv[1])
client = DaemonClient()
client.unlock()
client.set_user_app_access(uid, "approved")
client.set_user_can_install(uid, True)
try:
    client.install_app_for(uid, "com.spotify.Client")
    print("ACCEPTED")
except Exception as e:  # noqa: BLE001 - the message is the assertion
    print(e)
finally:
    client.set_user_can_install(uid, False)
PY
)
case "$refusal" in
    *"not on the approved"*) ok "unapproved apps are refused" ;;
    *) bad "unapproved app was not refused: $(printf '%s' "$refusal" | head -c 100)" ;;
esac
check "approved apps exist upstream" 0 kosherctl check-catalog

section "Direct installs are blocked"
# Pick something approved but NOT yet installed: flatpak short-circuits with
# "already installed" before it ever checks permission, which would make
# this pass for the wrong reason.
target=$(python3 - <<'PY'
from kosherd.client import DaemonClient
client = DaemonClient()
client.unlock()
installed = set(client.list_installed())
for app in client.list_catalog().get("apps", []):
    if app["ref"] not in installed:
        print(app["ref"])
        break
PY
)
if [ -z "$target" ]; then
    skip "every approved app is already installed; cannot test a fresh install"
else
    # This tests install authorization, independently of the site's network
    # permission. A whitelist refusal before Flatpak reaches polkit cannot
    # establish whether installation itself was denied.
    out=$(python3 - "$uid" "$target" <<'CHECK_INSTALL' 2>&1
import pwd, subprocess, sys
from kosherd.client import DaemonClient
uid, target = int(sys.argv[1]), sys.argv[2]
client = DaemonClient()
client.unlock()
original = next(u['mode'] for u in client.get_policy()['users'] if u['uid'] == uid)
user = pwd.getpwuid(uid)
prefix = ['runuser', '-u', user.pw_name, '--', 'env',
          'HOME=' + user.pw_dir, 'USER=' + user.pw_name, 'LOGNAME=' + user.pw_name]
try:
    client.set_filter_mode(uid, 'unfiltered')
    subprocess.run([*prefix, 'curl', '-fsS', '--max-time', '30', '-o', '/dev/null',
                    'https://dl.flathub.org/repo/summary.idx'], check=True)
    result = subprocess.run([*prefix, 'flatpak', 'install', '--system', '-y',
                             'flathub', target], capture_output=True, text=True, timeout=120)
    print(result.stdout + result.stderr)
finally:
    client.set_filter_mode(uid, original)
CHECK_INSTALL
)
    case "$out" in
        *"not allowed"*)
            ok "a user cannot install system-wide ($target)" ;;
        *) bad "system install was not refused: $(printf '%s' "$out" | tail -1)" ;;
    esac
fi
out=$(as wlkid flatpak install --user -y flathub org.gnome.Sudoku 2>&1)
case "$out" in
    *disallowed*|*"not allowed"*|*"No remote refs"*)
        ok "a user cannot install into their own scope" ;;
    *) bad "user-scope install was not refused: $(printf '%s' "$out" | tail -1)" ;;
esac

section "Per-user app permissions"
if ! command -v malcontent-client >/dev/null 2>&1; then
    bad "malcontent-client is missing"
    report
    exit 1
fi
check_contains "installs are disallowed for a managed user" "installation is disallowed" \
    malcontent-client get-app-filter "$uid"

mapfile -t installed < <(flatpak list --system --app --columns=application 2>/dev/null)
if [ "${#installed[@]}" -lt 2 ]; then
    skip "fewer than two apps installed; per-app filtering not exercised"
else
    allowed="${installed[0]}"
    blocked="${installed[1]}"
    python3 - "$uid" "$allowed" <<'PY'
import sys
from kosherd.client import DaemonClient
client = DaemonClient()
client.unlock()
client.set_user_apps(int(sys.argv[1]), [sys.argv[2]])
PY
    sleep 1
    check_contains "an allowed app stays runnable" "is allowed" \
        malcontent-client check-app-filter "$uid" "app/$allowed/$arch/stable"
    # The regression: a wildcard blocklist ref matched nothing, so apps the
    # admin had switched off stayed runnable.
    check_contains "an app outside the allow-list is blocked" "not allowed" \
        malcontent-client check-app-filter "$uid" "app/$blocked/$arch/stable"
    # Leave the user unrestricted again.
    python3 - "$uid" <<'PY'
import sys
from kosherd.client import DaemonClient
client = DaemonClient()
client.unlock()
client.set_user_apps(int(sys.argv[1]), [])
PY
fi

section "An app from its developer (Hex)"
# Hex brings a user service. The helper runs as the account exactly as
# kosherd runs it (same argv), against that account's own systemd: the
# install must leave the unit known to it, and removal must disable it.
# The unit tests stub systemctl out, so this is where it is exercised.
# Filtered, as a family's account is: it reaches Anomaly through the filter.
kctl set-mode "$uid" filtered >/dev/null
wait_for_proxy_as wlkid
loginctl enable-linger wlkid
for _ in $(seq 20); do [ -S "/run/user/$uid/systemd/private" ] && break; sleep 0.5; done
helper() {
    python3 - "$uid" "$@" <<'PY'
import subprocess, sys
from kosherd.upstreamapps import _argv
sys.exit(subprocess.run(_argv(int(sys.argv[1]), *sys.argv[2:])).returncode)
PY
}
user_systemctl() { systemctl --user -M wlkid@ "$@" 2>/dev/null; }
if ! helper install hex >/dev/null 2>&1; then
    bad "Hex did not install for an account (helper install hex)"
else
    check_contains "Hex's service is known to the account's systemd" "LoadState=loaded" \
        user_systemctl show -p LoadState hex.service
    # Hex enables it itself at onboarding; do the same, then remove.
    user_systemctl enable hex.service >/dev/null
    home=$(getent passwd wlkid | cut -d: -f6)
    as wlkid sh -c 'echo "{}" > ~/.local/share/hex/settings.json'
    check "Hex is removed for the account" 0 helper remove hex
    check_contains "removal disables Hex's service" "not-found" \
        user_systemctl show -p LoadState hex.service
    if compgen -G "$home/.config/systemd/user/*.wants/hex.service" >/dev/null; then
        bad "Hex's service is still enabled after removal"
    else
        ok "no enable link for Hex's service is left"
    fi
    if [ -e "$home/.local/share/hex/settings.json" ] && [ ! -e "$home/.local/bin/hex" ]; then
        ok "removal takes the program and keeps the account's settings"
    else
        bad "removal left the program or took the settings"
    fi
    rm -rf "$home/.local/share/hex"
fi
loginctl disable-linger wlkid
kctl set-mode "$uid" whitelist >/dev/null

report
