#!/usr/bin/env bash
# The approved-app allowlist: what may be installed, by whom, and who may run it.
set -uo pipefail
. "$(dirname "$0")/lib.sh"

ensure_test_users
uid=$(id -u wlkid)

section "Allowlist"
check_contains "the catalog is readable" "apps" kosherctl get-policy >/dev/null
check_contains "unapproved apps are refused" "not on the approved app list" \
    kosherctl install-app com.spotify.Client
check "approved apps exist upstream" 0 kosherctl check-catalog

section "Direct installs are blocked"
# polkit denies the flatpak system helper to every non-root caller...
check_contains "a user cannot install system-wide" "not allowed" \
    as wlkid flatpak install --system -y flathub org.gnome.Sudoku
# ...and malcontent blocks the user scope.
check_contains "a user cannot install into their own scope" "disallowed\|not allowed\|No remote refs" \
    as wlkid flatpak install --user -y flathub org.gnome.Sudoku

section "Per-user app permissions"
if command -v malcontent-client >/dev/null 2>&1; then
    check_contains "installs are disallowed for a managed user" "installation is disallowed" \
        malcontent-client get-app-filter "$uid"
    installed=$(flatpak list --system --app --columns=application 2>/dev/null | head -1)
    if [ -n "$installed" ]; then
        arch=$(flatpak --default-arch 2>/dev/null || echo x86_64)
        kosherctl set-user-apps "$uid" "$installed" >/dev/null 2>&1 || true
        sleep 1
        check_contains "an allowed app stays runnable" "is allowed" \
            malcontent-client check-app-filter "$uid" "app/$installed/$arch/stable"
        other=$(flatpak list --system --app --columns=application 2>/dev/null | sed -n 2p)
        if [ -n "$other" ]; then
            check_contains "an app outside the allow-list is blocked" "not allowed" \
                malcontent-client check-app-filter "$uid" "app/$other/$arch/stable"
        else
            skip "only one app installed; cannot test the blocked case"
        fi
        kosherctl set-user-apps "$uid" >/dev/null 2>&1 || true
    else
        skip "no apps installed; per-app filtering not exercised"
    fi
else
    bad "malcontent-client is missing"
fi

report
