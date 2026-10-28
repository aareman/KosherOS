#!/usr/bin/env bash
# keyd remapping per account (#165), on the desktop machine with PR_USER
# signed in at seat0: the person whose files are in force.
#
# Keys are typed on a uinput keyboard (keys.py), which keyd grabs like any
# other, and what keyd's virtual keyboard sends is what the remap did.
# Key codes: 45 x, 21 y, 46 c, 63 F5.
set -uo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
. "$here/../lib.sh"

u="${PR_USER:?PR_USER is the account signed in at the screen}"
uid=$(id -u "$u")
home=$(getent passwd "$u" | cut -d: -f6)
keys() { python3 "$here/keys.py" "$@"; }
# Write a file in the account's keyd folder as the account, the way a
# person saves one; their session's kosher-keyboard sees it and sends it.
save() {
    as "$u" sh -c "mkdir -p '$home/.config/keyd' && cat > '$home/.config/keyd/$1'"
}
person_file() { cat /run/keyd/person-default.conf 2>/dev/null; }
wait_until() {
    local n="$1"
    shift
    for _ in $(seq 1 "$n"); do
        "$@" && return 0
        sleep 1
    done
    return 1
}

section "Off until an administrator turns it on (#230)"
# A test machine has nobody with keyd files at kosherd's first start, so
# the switch settled off.
check_contains "kosherctl says key remapping is off" "keyd: off" kosherctl advanced keyd
check "keyd.service is not running" 1 systemctl is-active --quiet keyd
check "and does not start at boot" 1 systemctl is-enabled --quiet keyd
printf '[main]\nx = y\n' | save default.conf
if wait_until 20 sh -c "journalctl -b --no-pager _SYSTEMD_USER_UNIT=kosher-keyboard.service \
        _UID=$uid | grep -q 'switched off on this computer'"; then
    ok "the account's file is refused, saying key remapping is switched off"
else
    bad "kosher-keyboard did not report the switch: $(journalctl -b --no-pager _SYSTEMD_USER_UNIT=kosher-keyboard.service | tail -2)"
fi
check "and nothing reached /run/keyd" 1 test -f /run/keyd/person-default.conf
check "kosherctl turns it on" 0 kctl advanced keyd on
check_contains "the activity log records it" "SetKeydEnabled" kosherctl activity
if wait_until 20 sh -c 'grep -q "x = y" /run/keyd/person-default.conf 2>/dev/null'; then
    ok "switched on, the account's file is in force without signing out"
else
    bad "switched on, the account's file never reached /run/keyd: $(ls /run/keyd 2>&1)"
fi
as "$u" rm -f "$home/.config/keyd/default.conf"
wait_until 20 sh -c '! test -f /run/keyd/person-default.conf' || true

section "keyd is the only thing reading the keyboards"
check "keyd.service is running" 0 systemctl is-active --quiet keyd
members=$(getent group keyd | cut -d: -f4)
if getent group keyd >/dev/null && [ -z "$members" ]; then
    ok "the keyd group has no members"
else
    bad "the keyd group has members, or is missing: '$(getent group keyd)'"
fi
check "the machine's own file is in /run/keyd" 0 test -f /run/keyd/default.conf
check "the signed-in account's session runs kosher-keyboard" 0 \
    sh -c "systemctl --machine=$u@.host --user is-active --quiet kosher-keyboard.service"
out=$(keys 45)
if [ "$out" = 45 ]; then
    ok "with no files of the account's own, x is x"
else
    bad "with no files of the account's own, x came out as '$out'"
fi
check_contains "keyd grabs a keyboard as it appears" "pr-test keyboard" \
    journalctl -b -u keyd --no-pager -n 100

section "The account's own default.conf"
ran="$home/.kosher-keyd-ran"
rm -f "$ran"
printf '[main]\nx = y\nf5 = command(id -u > %s)\n' "$ran" | save default.conf
if wait_until 20 sh -c 'grep -q "x = y" /run/keyd/person-default.conf 2>/dev/null'; then
    ok "saving it puts it in /run/keyd, without signing out"
else
    bad "the file never reached /run/keyd: $(ls /run/keyd; journalctl -b --no-pager _SYSTEMD_USER_UNIT=kosher-keyboard.service | tail -3)"
fi
check "keyd never sees the command's text" 1 grep -q 'kosher-keyd-ran' /run/keyd/person-default.conf
check_contains "the key runs the fixed helper instead" "kosher-keyd-run $uid" cat /run/keyd/person-default.conf
check "the machine's file is replaced by theirs (it is for every keyboard)" 1 test -f /run/keyd/default.conf
check_contains "kosherd says whose files are in force" "in force for uid $uid" \
    journalctl -b -u kosherd --no-pager -n 200
out=$(keys 45)
if [ "$out" = 21 ]; then
    ok "keyd applies it: x comes out as y"
else
    bad "x came out as '$out', not y (21)"
fi

section "command() runs as the person"
keys 63 >/dev/null
if wait_until 10 test -s "$home/.kosher-keyd-ran"; then
    owner=$(stat -c %U "$home/.kosher-keyd-ran")
    ran_as=$(cat "$home/.kosher-keyd-ran")
    if [ "$ran_as" = "$uid" ] && [ "$owner" = "$u" ]; then
        ok "F5's command ran as $u (uid $uid), not root"
    else
        bad "F5's command ran as uid '$ran_as' (file owned by $owner)"
    fi
else
    bad "F5's command did not run: $(journalctl -b --no-pager -t kosher-keyd-run | tail -3)"
fi

section "A bad file is refused and the last good one stays"
# keyd check says "WARNING: <path>:4: invalid key or action" for this line;
# kosherd takes the two header lines off and names the person's own line.
watcher_pid=$(systemctl --machine="$u@.host" --user show kosher-keyboard.service -p MainPID --value)
since=$(date '+%Y-%m-%d %H:%M:%S')
printf '[main]\nx = definitely_not_a_key\n' | save default.conf
kosherd_log() { journalctl -b --no-pager -u kosherd --since "$since" -o cat; }
watcher_log() {
    journalctl -b --no-pager --since "$since" -o cat _SYSTEMD_USER_UNIT=kosher-keyboard.service
}
kosherd_refused() { kosherd_log | grep -q "SetMyKeyboard.*refused"; }
wait_until 20 kosherd_refused || true
if kosherd_log | grep -q "refused: default.conf line 2: invalid key or action"; then
    ok "kosherd refuses it, naming the file and line"
else
    bad "kosherd did not refuse it by file and line: $(kosherd_log | grep -i keyboard | tail -3 | tr '\n' '|')"
fi
sleep 2
if watcher_log | grep -q "not applied: default.conf line 2"; then
    ok "kosher-keyboard reports it, naming the file and line"
else
    bad "kosher-keyboard did not report it: $(watcher_log | tail -4 | tr '\n' '|' | head -c 400)"
fi
restarts=$(systemctl --machine="$u@.host" --user show kosher-keyboard.service -p NRestarts --value)
now_pid=$(systemctl --machine="$u@.host" --user show kosher-keyboard.service -p MainPID --value)
if [ "$restarts" = 0 ] && [ "$now_pid" = "$watcher_pid" ]; then
    ok "and keeps running (pid $watcher_pid, no restart)"
else
    bad "kosher-keyboard restarted after the refusal (restarts=$restarts, pid $watcher_pid -> $now_pid)"
fi
check_contains "the previous file is still in force" "x = y" person_file
out=$(keys 45)
if [ "$out" = 21 ]; then
    ok "and keyd still remaps x to y"
else
    bad "after the bad file x came out as '$out'"
fi
printf '[main]\nx = c\nf5 = command(id -u > %s)\n' "$ran" | save default.conf
if wait_until 20 sh -c 'grep -q "x = c" /run/keyd/person-default.conf 2>/dev/null'; then
    ok "a good file after it applies again"
else
    bad "the corrected file was not applied"
fi

section "Another session on the screen resets the keys"
rm -f "$home/.kosher-keyd-ran"
# Signing out is a session switch to the login screen: nobody's files.
for s in $(loginctl list-sessions --no-legend | awk -v u="$u" '$3 == u {print $1}'); do
    loginctl terminate-session "$s"
done
if wait_until 30 test -f /run/keyd/default.conf; then
    ok "after signing out the machine's own file is back"
else
    bad "the machine's file did not come back: $(ls /run/keyd)"
fi
check "and the account's file is gone" 1 test -f /run/keyd/person-default.conf
out=$(keys 45 63)
if [ "$out" = "45 63" ]; then
    ok "at the login screen x is x and F5 is F5"
else
    bad "at the login screen x, F5 came out as '$out'"
fi
sleep 2
check "and F5 no longer runs the account's command" 1 test -e "$home/.kosher-keyd-ran"
as "$u" rm -f "$home/.config/keyd/default.conf"

section "Switched off again"
check "kosherctl turns it off" 0 kctl advanced keyd off
check "keyd.service is stopped" 1 systemctl is-active --quiet keyd
check "and does not start at boot" 1 systemctl is-enabled --quiet keyd
# keys.py reads what keyd's virtual keyboard sends; with keyd stopped
# there is none, and nothing between the keyboard and the desktop.
out=$(keys 45)
case "$out" in
    *"no keyd virtual keyboard"*) ok "with keyd stopped nothing stands between the keyboard and the desktop" ;;
    *) bad "with keyd stopped its virtual keyboard is still there (got '$out')" ;;
esac

report
