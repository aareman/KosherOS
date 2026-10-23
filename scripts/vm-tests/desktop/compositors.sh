#!/usr/bin/env bash
# Noctalia on niri, Hyprland or Umbriel (#167), on the desktop machine with
# PR_USER signed in at seat0 (run by the desktop stage of `just pr-test`).
#
# The session files, the configs and the choice are checked on any machine.
# Signing in to each compositor needs a GPU, so that part runs only when
# the harness gave the machine one (`just pr-test --gl`, PR_GL=1); without
# it the run says so.
set -uo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
. "$here/../lib.sh"

u="${PR_USER:?PR_USER is the account signed in at the screen}"
. "$here/indicator-lib.bash"
uid=$(id -u "$u")
# A command inside the account's own service manager, so it has the
# session's bus and polkit counts it as the person at the screen.
in_session() {
    runuser -u "$u" -- env XDG_RUNTIME_DIR="/run/user/$uid" \
        DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$uid/bus" \
        systemd-run --user --quiet --wait --pipe --collect -- "$@"
}
user_systemctl() { systemctl --machine="$u@.host" --user "$@"; }
seat_session() {
    loginctl list-sessions --no-legend | awk -v u="$u" '$3 == u && $4 == "seat0" {print $1; exit}'
}
active_on_seat() {
    local s
    s=$(seat_session)
    [ -n "$s" ] && [ "$(loginctl show-session "$s" -p Active --value)" = yes ]
}
wait_until() {
    local n="$1"
    shift
    for _ in $(seq 1 "$n"); do
        "$@" && return 0
        sleep 1
    done
    return 1
}
set_compositor() {
    in_session python3 -c "from kosherd.client import DaemonClient; DaemonClient().set_my_compositor('$1')"
}

section "Each compositor has a session and a config that parses"
for c in niri hyprland umbriel; do
    check "the $c session file exists" 0 test -f "/usr/share/wayland-sessions/$c.desktop"
done
check "Hyprland is installed" 0 command -v Hyprland
# As the person, not root: Hyprland refuses to start, even to check a
# config, when it was started with root's privileges ("launched with su").
check_contains "Hyprland --verify-config accepts /etc/xdg/hypr/hyprland.lua" "config ok" \
    in_session Hyprland --verify-config -c /etc/xdg/hypr/hyprland.lua
check "umbriel is installed" 0 command -v umbriel
check_contains "umbriel config validate accepts /etc/xdg/umbriel/config.toml" "config: ok" \
    umbriel config validate -c /etc/xdg/umbriel/config.toml
check "the Hyprland session target is installed" 0 \
    test -f /usr/lib/systemd/user/kosher-hyprland-session.target
check_contains "the Look app lists all three" "niri hyprland umbriel" \
    python3 -c 'from kosherlook.app import installed_compositors; print(*installed_compositors())'

section "The person picks one"
kosherctl layout "$uid" advanced >/dev/null
for c in hyprland umbriel niri; do
    if set_compositor "$c" >/dev/null 2>&1; then
        ok "$u sets their compositor to $c from their session"
    else
        bad "$u could not set their compositor to $c"
    fi
    check_contains "and the login screen is pointed at the $c session" "\"$c\"" \
        busctl get-property org.freedesktop.Accounts "/org/freedesktop/Accounts/User$uid" \
        org.freedesktop.Accounts.User Session
done
check_contains "kosherd keeps the choice" "niri" \
    in_session python3 -c 'from kosherd.client import DaemonClient; print(DaemonClient().my_compositor())'

if [ "${PR_GL:-0}" != 1 ]; then
    skip "signing in to niri, Hyprland and Umbriel needs a GPU: run with --gl (KOSHER_GL=1)"
    kosherctl layout "$uid" classic >/dev/null
    report
    exit $?
fi

section "Signing in to each one"
# GDM signs the account in by itself (automatic login) into the session
# accountsservice names, the one the login screen would preselect.
[ -f /etc/gdm/custom.conf ] && cp -a /etc/gdm/custom.conf /run/pr-test-gdm-custom.conf
for c in hyprland umbriel niri; do
    set_compositor "$c" >/dev/null 2>&1
    for s in $(loginctl list-sessions --no-legend | awk -v u="$u" '$3 == u {print $1}'); do
        loginctl terminate-session "$s"
    done
    wait_until 30 sh -c "! loginctl list-sessions --no-legend | awk '\$3 == \"$u\"' | grep -q ."
    printf '[daemon]\nWaylandEnable=true\nAutomaticLoginEnable=True\nAutomaticLogin=%s\n' "$u" \
        > /etc/gdm/custom.conf
    systemctl restart gdm
    if ! wait_until 90 active_on_seat; then
        bad "$c: $u is not signed in on seat0 after an automatic login"
        continue
    fi
    # Which session this is: the compositor names itself in the session's
    # XDG_CURRENT_DESKTOP (logind's Desktop property stays empty under an
    # automatic login), once it has imported its environment.
    sleep 15
    desktop=$(user_systemctl show-environment 2>/dev/null | sed -n 's/^XDG_CURRENT_DESKTOP=//p')
    if printf '%s' "$desktop" | grep -qi "$c"; then
        ok "$c: the session GDM starts is $c (XDG_CURRENT_DESKTOP=$desktop)"
    else
        bad "$c: the session GDM starts is not $c (XDG_CURRENT_DESKTOP='$desktop')"
    fi
    proc=$c
    [ "$c" = hyprland ] && proc=Hyprland
    if wait_until 60 pgrep -u "$uid" -x "$proc" >/dev/null; then
        ok "$c: the compositor is running"
    else
        bad "$c: no $proc process for $u"
    fi
    if wait_until 60 sh -c "pgrep -u $uid -f noctalia >/dev/null"; then
        ok "$c: Noctalia is running"
    else
        bad "$c: Noctalia did not start"
    fi
    sleep 10
    check "$c: graphical-session.target is active" 0 user_systemctl is-active --quiet graphical-session.target
    started=$(user_systemctl show kosher-layout.service -p ExecMainStartTimestamp --value 2>/dev/null)
    if [ -n "$started" ]; then
        ok "$c: kosher-layout ran ($started)"
    else
        bad "$c: kosher-layout did not run"
    fi
    # The update mark (#19) in Noctalia's tray, as on GNOME.
    indicator_check "$c"
    failed=$(user_systemctl list-units --failed --no-legend --plain 2>/dev/null)
    if [ -z "$failed" ]; then
        ok "$c: none of the account's user services failed"
    else
        bad "$c: failed user services: $(printf '%s' "$failed" | tr '\n' ' ' | head -c 200)"
    fi
done
if [ -f /run/pr-test-gdm-custom.conf ]; then
    cp -a /run/pr-test-gdm-custom.conf /etc/gdm/custom.conf
else
    rm -f /etc/gdm/custom.conf
fi
kosherctl layout "$uid" classic >/dev/null

report
