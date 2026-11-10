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

report
