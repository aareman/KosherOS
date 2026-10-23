# shellcheck shell=bash
# The "update ready" mark in the panel (#19), for the desktop scripts:
# sourced by update-indicator.sh and compositors.sh, not run on its own
# (the desktop stage runs *.sh only).
#
# Needs $u (the account at the screen) and lib.sh's ok/bad. The mark is
# kosher-update-notify's StatusNotifierItem; the tray that shows it (GNOME's
# AppIndicator extension, or Noctalia's tray) owns the session bus name
# org.kde.StatusNotifierWatcher and lists the items it shows.

AUTO_STATE=/var/lib/kosher/auto-update.json
: "${u:?indicator.bash needs u, the account at the screen}"

indicator_busctl() { busctl --user --machine="$u@.host" "$@"; }

indicator_name() {
    local pid
    pid=$(systemctl --machine="$u@.host" --user show kosher-update-notify.service \
        -p MainPID --value 2>/dev/null)
    [ -n "$pid" ] && [ "$pid" != 0 ] && printf 'org.kde.StatusNotifierItem-%s-1' "$pid"
}

# Does the tray list the mark? Either by the name it registered or by the
# unique name that owns it, as trays differ in which they keep.
indicator_shown() {
    local name owner items
    name=$(indicator_name) || return 1
    owner=$(indicator_busctl call org.freedesktop.DBus /org/freedesktop/DBus \
        org.freedesktop.DBus GetNameOwner s "$name" 2>/dev/null | awk '{print $2}' | tr -d '"')
    [ -n "$owner" ] || return 1
    items=$(indicator_busctl get-property org.kde.StatusNotifierWatcher /StatusNotifierWatcher \
        org.kde.StatusNotifierWatcher RegisteredStatusNotifierItems 2>/dev/null)
    printf '%s' "$items" | grep -q -e "$name" -e "$owner"
}

indicator_owned() {
    local name
    name=$(indicator_name) || return 1
    indicator_busctl call org.freedesktop.DBus /org/freedesktop/DBus org.freedesktop.DBus \
        NameHasOwner s "$name" 2>/dev/null | grep -q true
}

indicator_gone() { ! indicator_owned; }

indicator_tray() {
    indicator_busctl call org.freedesktop.DBus /org/freedesktop/DBus org.freedesktop.DBus \
        NameHasOwner s org.kde.StatusNotifierWatcher 2>/dev/null | grep -q true
}

# Pretend kosherd noted a staged update this boot, or that none waits.
# The real note is kept aside and put back by indicator_restore. kosherd
# also looks for ostree's own marker of a staged deployment, so the check
# makes one when there is none and takes it away again; this machine is
# thrown away after the run, and nothing is staged on it.
STAGED_MARK=/run/ostree/staged-deployment

indicator_stage() {
    [ -f "$AUTO_STATE" ] && [ ! -f /run/pr-test-auto-update.json ] \
        && cp -a "$AUTO_STATE" /run/pr-test-auto-update.json
    if [ ! -e "$STAGED_MARK" ]; then
        mkdir -p /run/ostree && : > "$STAGED_MARK" && touch /run/pr-test-made-staged-mark
    fi
    python3 - <<'PY'
from kosherd import updates
state = updates.load_state()
updates.record_staged(state, "9.9.9-indicator", "sha256:indicator-test",
                      updates.boot_id(), announce=False)
updates.save_state(state)
PY
}

indicator_clear() {
    if [ -f /run/pr-test-made-staged-mark ]; then
        rm -f "$STAGED_MARK" /run/pr-test-made-staged-mark
    fi
    python3 -c 'from kosherd import updates; s = updates.load_state(); updates.clear_staged(s); updates.save_state(s)'
}

indicator_restore() {
    if [ -f /run/pr-test-auto-update.json ]; then
        mv -f /run/pr-test-auto-update.json "$AUTO_STATE"
    else
        rm -f "$AUTO_STATE"
    fi
}

indicator_restart_helper() {
    systemctl --machine="$u@.host" --user restart kosher-update-notify.service
    sleep 3
}

indicator_wait() {
    for _ in $(seq 1 "$1"); do
        "${@:2}" && return 0
        sleep 1
    done
    return 1
}

# The whole check, under a label ("GNOME", "niri", ...).
indicator_check() {
    local where="$1"
    if ! indicator_wait 30 indicator_tray; then
        local uid names ext
        uid=$(id -u "$u")
        names=$(indicator_busctl list --no-legend 2>&1 | awk '{print $1}' | grep -i -E 'notifier|watcher|^org\.gnome\.Shell' | tr '\n' ' ')
        ext=$(runuser -u "$u" -- env XDG_RUNTIME_DIR="/run/user/$uid" \
            DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$uid/bus" \
            gnome-extensions info appindicatorsupport@rgcjonas.gmail.com 2>&1 | tr '\n' ' ' | head -c 400)
        bad "$where: no tray (org.kde.StatusNotifierWatcher) on $u's session bus; names: ${names:-none}; appindicator: ${ext:-?}"
        return
    fi
    indicator_clear
    indicator_restart_helper
    if systemctl --machine="$u@.host" --user is-active --quiet kosher-update-notify.service; then
        ok "$where: kosher-update-notify runs in $u's session"
    else
        bad "$where: kosher-update-notify is not running in $u's session"
        indicator_restore
        return
    fi
    if indicator_owned; then
        bad "$where: the update mark shows with no update waiting"
    else
        ok "$where: no update waiting, no mark"
    fi
    indicator_stage
    indicator_restart_helper
    if indicator_wait 20 indicator_shown; then
        ok "$where: with an update waiting, the tray shows the mark (asked kosherd as $u)"
    else
        bad "$where: an update waits and the tray does not list the mark"
    fi
    indicator_clear
    indicator_restart_helper
    if indicator_wait 10 indicator_gone; then
        ok "$where: once nothing waits, the mark is gone"
    else
        bad "$where: the mark stayed after the update went"
    fi
    indicator_restore
    indicator_restart_helper
}
