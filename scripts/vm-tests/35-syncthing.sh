#!/usr/bin/env bash
# Syncthing from the Store, as a service one account runs (#213).
#
# What is checked: a filtered account gets it only once an administrator
# allows it for that account; its service runs and its page answers on
# loopback, to its owner only; the direct ports are let out for THAT
# service and for nothing else the account runs (a shell, or a second
# Syncthing started from one); the home network reaches it only with the
# switch on, and the internet never; taking the permission away stops it;
# uninstalling takes away the unit and the launcher and keeps the config.
#
# A test "internet" and a test "home network" are two network namespaces
# on veth pairs: 203.0.113.0/24 (TEST-NET-3, not a LAN range) and
# 192.168.77.0/24 (a LAN range). Pairing with a real second device is not
# tried here.
set -uo pipefail
. "$(dirname "$0")/lib.sh"

REF="nixpkgs#syncthing"
A=stkid      # filtered, gets Syncthing
B=stkid2     # filtered, does not
for u in "$A" "$B"; do
    id "$u" >/dev/null 2>&1 || useradd -m "$u"
    kosherctl get-policy 2>/dev/null | grep -q "\"$u\"" || kosherctl adopt "$u" filtered >/dev/null 2>&1
    kctl set-mode "$(id -u "$u")" filtered >/dev/null
done
uid=$(id -u "$A")
uid2=$(id -u "$B")
python3 - "$uid" "$uid2" <<'PY' >/dev/null 2>&1
import sys
from kosherd.client import DaemonClient
c = DaemonClient()
for uid in sys.argv[1:]:
    c.set_user_can_install(int(uid), True)
PY
wait_for_dns >/dev/null 2>&1 || true

install_for() {
    python3 - "$1" "$REF" <<'PY' 2>&1
import sys
from kosherd.client import DaemonClient
try:
    DaemonClient().install_app_for(int(sys.argv[1]), sys.argv[2])
    print("QUEUED")
except Exception as e:  # noqa: BLE001 - the message is the assertion
    print(e)
PY
}
registry() { python3 -c "import json,sys; e=json.load(open('/var/lib/kosher/syncthing/accounts.json')).get('$A',{}); print(e.get(sys.argv[1],''))" "$1" 2>/dev/null; }
in_set() { nft list set inet kosher "$1" 2>/dev/null | grep -q "syncthing@$A.service"; }
# connect_as <user> <host> <port>: 0 when a TCP connection was made.
connect_as() {
    as "$1" python3 -c 'import socket,sys; socket.create_connection((sys.argv[1], int(sys.argv[2])), 4)' "$2" "$3" >/dev/null 2>&1
}
connect_from() {   # connect_from <netns> <host> <port>
    ip netns exec "$1" python3 -c 'import socket,sys; socket.create_connection((sys.argv[1], int(sys.argv[2])), 4)' "$2" "$3" >/dev/null 2>&1
}

section "Test networks"
cleanup_ns() {
    for ns in stwan stlan; do
        ip netns pids "$ns" 2>/dev/null | xargs -r kill 2>/dev/null
        ip netns del "$ns" 2>/dev/null
    done
    ip link del vstwan 2>/dev/null
    ip link del vstlan 2>/dev/null
}
cleanup_ns
trap cleanup_ns EXIT
ok_ns=1
while read -r ns dev net; do
    ip netns add "$ns" && ip link add "$dev" type veth peer name "${dev}p" \
        && ip link set "${dev}p" netns "$ns" \
        && ip addr add "$net.1/24" dev "$dev" && ip link set "$dev" up \
        && ip netns exec "$ns" ip addr add "$net.2/24" dev "${dev}p" \
        && ip netns exec "$ns" ip link set "${dev}p" up \
        && ip netns exec "$ns" ip link set lo up || ok_ns=0
done <<'NETS'
stwan vstwan 203.0.113
stlan vstlan 192.168.77
NETS
if [ "$ok_ns" = 1 ]; then ok "two test networks: an internet one and a home one"; else bad "could not make the test networks"; fi
# The "internet" listener on 22000, logging who connected.
WANLOG=/tmp/st-wan.log
: > "$WANLOG"
ip netns exec stwan python3 - "$WANLOG" <<'PY' &
import socket, sys
s = socket.socket()
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind(("203.0.113.2", 22000))
s.listen(16)
while True:
    conn, peer = s.accept()
    with open(sys.argv[1], "a") as log:
        log.write(f"{peer[0]}:{peer[1]}\n")
    conn.close()
PY
sleep 1

section "A filtered account needs its own approval"
out=$(install_for "$uid")
case "$out" in
    *"waiting for an administrator"*) ok "refused until an administrator allows it for the account" ;;
    *) bad "installed without approval: $(printf '%s' "$out" | tail -1 | head -c 160)" ;;
esac
check "an administrator allows it for that account" 0 kctl apps approve "$uid" "$REF"
check_contains "the activity log records the approval" "SetUserAppApproval" \
    kosherctl activity --user "$A"
out=$(install_for "$uid")
case "$out" in
    *QUEUED*) ok "and then it installs for that account" ;;
    *) bad "the install was refused: $(printf '%s' "$out" | tail -1 | head -c 160)" ;;
esac
for _ in $(seq 1 180); do
    systemctl is-active --quiet "syncthing@$A.service" && break
    sleep 5
done
if systemctl is-active --quiet "syncthing@$A.service"; then
    ok "syncthing@$A.service runs"
else
    bad "the service is not running"
    journalctl -u kosherd -u "syncthing@$A.service" -n 40 --no-pager 2>&1 | sed 's/^/    /'
    # What stops it, one sandboxing setting at a time.
    sbin=$(find /nix/store -maxdepth 3 -path "/nix/store/*-syncthing-[0-9]*/bin/syncthing" 2>/dev/null | head -1)
    stat -c "%n %U %a %C" "$sbin" 2>&1 | sed 's/^/    /'
    for props in "" "-p PrivateUsers=true" "-p NoExecPaths=/ -p ExecPaths=/nix/store" \
                 "-p ProtectSystem=full" "-p MemoryDenyWriteExecute=true" \
                 "-p SystemCallFilter=@system-service" "-p PrivateDevices=true" \
                 "-p RestrictNamespaces=true" "-p NoNewPrivileges=true"; do
        # shellcheck disable=SC2086
        r=$(systemd-run --quiet --wait --pipe -p User="$A" $props "$sbin" --version 2>&1 | tail -1)
        printf '    [%s] %s\n' "${props:-plain}" "$r"
    done
    ausearch -m avc -ts recent 2>/dev/null | tail -8 | sed 's/^/    /'
    journalctl -t audit -t kernel --since -5min --no-pager 2>/dev/null | grep -i avc | tail -8 | sed 's/^/    /'
fi
dropin=/etc/systemd/system/syncthing@$A.service.d/50-kosheros.conf
check_contains "a system unit, run as the account" "User=%i" systemctl cat "syncthing@$A.service"
check_contains "from a Nix store path kosherd built" "ExecStart=/nix/store/" cat "$dropin"
check "the account cannot write the unit" 1 as "$A" sh -c "echo x >> '$dropin'"
launcher=/home/$A/.local/share/applications/kosheros-syncthing.desktop
gui=$(registry gui)
listen=$(registry listen)
check_contains "a launcher that opens its page" "http://127.0.0.1:$gui/" cat "$launcher"
check "the launcher is the account's own file" 0 test "$(stat -c %U "$launcher")" = "$A"
code=000
for _ in $(seq 1 30); do
    code=$(as "$A" curl -s -o /dev/null -w '%{http_code}' --max-time 5 "http://127.0.0.1:$gui/" 2>/dev/null)
    [ "$code" = 200 ] && break
    sleep 2
done
if [ "$code" = 200 ]; then ok "its page answers on 127.0.0.1:$gui"; else bad "its page answered $code"; fi
code=$(as "$B" curl -s -o /dev/null -w '%{http_code}' --max-time 5 "http://127.0.0.1:$gui/" 2>/dev/null)
if [ "$code" = 000 ]; then ok "another account cannot open it"; else bad "another account got $code from it"; fi
pid=$(systemctl show -p MainPID --value "syncthing@$A.service")
check "the account cannot read into the service's process" 1 as "$A" cat "/proc/$pid/environ"
check "its cgroup is in the set that lets its ports out" 0 in_set syncthing_out

section "Port 22000 is Syncthing's alone"
before=$(wc -l < "$WANLOG")
check "a program in the approved account's shell is refused on 22000" 1 connect_as "$A" 203.0.113.2 22000
check "and on 22067" 1 connect_as "$A" 203.0.113.2 22067
check "an account without it is refused on 22000" 1 connect_as "$B" 203.0.113.2 22000
sleep 1
if [ "$(wc -l < "$WANLOG")" = "$before" ]; then ok "none of those reached the internet listener"; else bad "a connection from outside the service reached the listener"; fi

# A second Syncthing, started by the account from its own shell, told to
# dial the same listener.
bin=$(sed -n 's|^ExecStart=\(/nix/store/[^ ]*/bin/syncthing\).*|\1|p' "$dropin")
devhome=$(mktemp -d)
"$bin" generate --home="$devhome" --no-port-probing >/dev/null 2>&1
devid=$("$bin" device-id --home="$devhome" 2>/dev/null | tail -1)
if [ -z "$devid" ]; then
    devid=$(sed -n 's|.*<device id="\([A-Z0-9-]*\)".*|\1|p' "$devhome/config.xml" | head -1)
fi
rm -rf "/tmp/$A-second"
as "$A" "$bin" generate --home="/tmp/$A-second" --no-port-probing >/dev/null 2>&1
as "$A" env STNODEFAULTFOLDER=1 "$bin" serve --home="/tmp/$A-second" --no-browser --no-restart \
    --no-upgrade --gui-address=http://127.0.0.1:18999 --gui-apikey=second-key \
    > /tmp/st-second.log 2>&1 &
second=$!
add_device() {   # add_device <gui port> <api key>
    for _ in $(seq 1 30); do
        curl -s -o /dev/null --max-time 3 -H "X-API-Key: $2" "http://127.0.0.1:$1/rest/system/ping" && break
        sleep 1
    done
    curl -s -X PATCH -H "X-API-Key: $2" -H 'Content-Type: application/json' \
        -d '{"reconnectionIntervalS": 5}' "http://127.0.0.1:$1/rest/config/options" >/dev/null
    curl -s -X POST -H "X-API-Key: $2" -H 'Content-Type: application/json' \
        -d "{\"deviceID\": \"$devid\", \"addresses\": [\"tcp://203.0.113.2:22000\"]}" \
        "http://127.0.0.1:$1/rest/config/devices" >/dev/null
}
add_device 18999 second-key
sleep 30
kill "$second" 2>/dev/null
pkill -u "$A" -f "$A-second" 2>/dev/null
if [ "$(wc -l < "$WANLOG")" = "$before" ]; then ok "a second Syncthing from the account's shell gets no direct port"; else bad "the shell's own Syncthing reached the listener"; fi

# The service itself, told to dial the listener.
apikey=$(sed -n 's|.*<apikey>\(.*\)</apikey>.*|\1|p' "/home/$A/.local/state/syncthing/config.xml")
add_device "$gui" "$apikey"
for _ in $(seq 1 45); do
    [ "$(wc -l < "$WANLOG")" -gt "$before" ] && break
    sleep 2
done
if [ "$(wc -l < "$WANLOG")" -gt "$before" ]; then ok "the Syncthing service connects out on 22000"; else bad "the service did not reach the listener on 22000"; fi

section "The service keeps its ports through a restart and a reload"
systemctl restart "syncthing@$A.service"
sleep 3
check "after a restart its new cgroup is in the set" 0 in_set syncthing_out
kctl network "$uid" --video-calls off >/dev/null
kctl network "$uid" --video-calls on >/dev/null
wait_for_dns >/dev/null 2>&1 || true
check "after the ruleset is replaced it is in the set again" 0 in_set syncthing_out

section "The home network, only with the switch on"
check "off: a device on the home network cannot connect" 1 connect_from stlan 192.168.77.1 "$listen"
check "kosherctl turns it on" 0 kctl apps syncthing-lan "$uid" on
check_contains "the activity log records it" "SetSyncthingLan" kosherctl activity --user "$A"
sleep 2
check "on: a device on the home network connects" 0 connect_from stlan 192.168.77.1 "$listen"
check "and nothing from the internet does" 1 connect_from stwan 203.0.113.1 "$listen"
check "off again" 0 kctl apps syncthing-lan "$uid" off
sleep 2
check "and the home network is refused again" 1 connect_from stlan 192.168.77.1 "$listen"

section "Taking the permission away, and removing it"
check "an administrator takes the permission away" 0 kctl apps unapprove "$uid" "$REF"
sleep 2
check "the service stops" 1 systemctl is-active --quiet "syncthing@$A.service"
check "and its cgroup is out of the set" 1 in_set syncthing_out
python3 -c 'from kosherd.client import DaemonClient; DaemonClient().remove_app("nixpkgs#syncthing")' >/dev/null 2>&1
for _ in $(seq 1 30); do
    [ -e "$dropin" ] || break
    sleep 2
done
check "uninstalling removes the unit's drop-in" 1 test -e "$dropin"
check "and the launcher" 1 test -e "$launcher"
check "and keeps Syncthing's own settings" 0 test -f "/home/$A/.local/state/syncthing/config.xml"

report
