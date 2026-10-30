#!/usr/bin/env bash
# The dev VM: master's set-up machine, booted for keeps, to push code into.
#
#   dev-vm.sh up       boot it, with a window (DEV_VM_HEADLESS=1 for none);
#                      a root shell on 127.0.0.1:2223
#   dev-vm.sh ssh      shell into it (also: ssh -F build/vm/ssh_config kosher-dev)
#   dev-vm.sh status
#   dev-vm.sh down     power off; the machine keeps its state for the next up
#   dev-vm.sh fresh    throw the machine away; the next up starts from the base again
#
# The machine is an overlay of the base `just pr-test` keeps in
# ~/.cache/kosher-pr-test (KOSHER_PR_TEST_CACHE): master's image, installed
# and set up by the wizard. The base is never written. No base yet? Any
# `just pr-test` run makes one. The machine runs MASTER's image: this
# checkout's code goes in with `just deploy` (bootc usr-overlay + rsync),
# which is the point — no build, no install, seconds per change. A change
# to the Containerfile itself (a package, a build step) is the one thing a
# deploy cannot carry: `just build && just vm-upgrade` updates this machine
# to the built image instead.
#
# The port is `just boot-image`'s, so `just vm-upgrade` and the kosher-gui
# recipes reach this machine too; one of the two runs at a time.
set -euo pipefail

repo="$(cd "$(dirname "$0")/.." && pwd)"
vm_dir="$repo/build/dev-vm"
ssh_dir="$repo/build/vm"
ssh_config="$ssh_dir/ssh_config"
cache="${KOSHER_PR_TEST_CACHE:-${XDG_CACHE_HOME:-$HOME/.cache}/kosher-pr-test}"
key="$cache/id_ed25519"
machine="$vm_dir/machine.qcow2"
pidfile="$vm_dir/qemu.pid"
port="${KOSHER_DEV_VM_PORT:-2223}"

mkdir -p "$vm_dir" "$ssh_dir"

die() { echo "dev-vm: $*" >&2; exit 1; }

newest_base() {
    # The base set up most recently; its ssh key is the cache's.
    local setup
    setup=$(find "$cache" -maxdepth 2 -path '*/base-*/setup.qcow2' -printf '%T@ %p\n' 2>/dev/null \
            | sort -rn | head -1 | cut -d' ' -f2-)
    [ -n "$setup" ] || die "no set-up base in $cache; a \`just pr-test HEAD --shard desktop\` makes one"
    [ -f "$key" ] || die "no $key: the base's ssh key; was the cache moved?"
    echo "$setup"
}

# A block in build/vm/ssh_config, kept apart from the stage-1 VM's by its
# markers so either script can rewrite its own without losing the other's.
write_ssh_config() {
    local tmp="$ssh_config.tmp"
    touch "$ssh_config"
    awk '/^# >>> kosher-dev/{skip=1} !skip{print} /^# <<< kosher-dev/{skip=0}' "$ssh_config" > "$tmp"
    cat >> "$tmp" <<EOF
# >>> kosher-dev (scripts/dev-vm.sh writes this block)
Host kosher-dev
    HostName 127.0.0.1
    Port $port
    User root
    IdentityFile $key
    IdentitiesOnly yes
    StrictHostKeyChecking no
    UserKnownHostsFile /dev/null
    LogLevel ERROR
# <<< kosher-dev
EOF
    mv "$tmp" "$ssh_config"
}

running() {
    [ -f "$pidfile" ] && kill -0 "$(cat "$pidfile")" 2>/dev/null
}

own_pubkey() {
    for k in ~/.ssh/id_ed25519.pub ~/.ssh/id_rsa.pub; do
        [ -f "$k" ] && { cat "$k"; return; }
    done
    true
}

cmd_up() {
    if running; then
        echo "already running (pid $(cat "$pidfile")); ssh -F $ssh_config kosher-dev"
        return
    fi
    if ssh -o ConnectTimeout=2 -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
           -o LogLevel=ERROR -p "$port" root@127.0.0.1 true 2>/dev/null; then
        die "something already answers on 127.0.0.1:$port (just boot-image?); stop it first"
    fi
    local base
    base=$(newest_base)
    if [ ! -f "$machine" ]; then
        echo "a new machine from $(dirname "$base")"
        qemu-img create -q -f qcow2 -b "$base" -F qcow2 "$machine"
    fi
    write_ssh_config
    local display=(-device virtio-vga -display "${KOSHER_DISPLAY:-gtk}")
    [ -z "${DEV_VM_HEADLESS:-}" ] || display=(-display none)
    qemu-system-x86_64 \
        -enable-kvm -cpu host -m 4096 -smp 4 \
        -drive file="$machine",if=virtio \
        -netdev user,id=n0,hostfwd=tcp:127.0.0.1:"$port"-:22 \
        -device virtio-net-pci,netdev=n0 \
        "${display[@]}" -serial "file:$vm_dir/console.log" \
        -daemonize -pidfile "$pidfile"
    echo -n "booting"
    for _ in $(seq 90); do
        if ssh -F "$ssh_config" -o ConnectTimeout=2 kosher-dev true 2>/dev/null; then
            echo
            # Root takes the developer's own key as well, so plain
            # `ssh -p 2223 root@127.0.0.1` and `just vm-upgrade` work here.
            local pub
            pub=$(own_pubkey)
            if [ -n "$pub" ]; then
                ssh -F "$ssh_config" kosher-dev "grep -qxF '$pub' /root/.ssh/authorized_keys 2>/dev/null \
                    || echo '$pub' >> /root/.ssh/authorized_keys"
            fi
            echo "up — ssh -F build/vm/ssh_config kosher-dev; then: just deploy, just test-vm kosher-dev 20"
            return
        fi
        echo -n "."; sleep 2
    done
    echo; echo "the machine did not answer ssh; see $vm_dir/console.log" >&2
    exit 1
}

case "${1:-}" in
    up) cmd_up ;;
    ssh) shift; exec ssh -F "$ssh_config" kosher-dev "$@" ;;
    status) running && echo "running (pid $(cat "$pidfile"))" || echo "stopped" ;;
    down)
        if running; then
            ssh -F "$ssh_config" -o ConnectTimeout=3 kosher-dev systemctl poweroff 2>/dev/null || true
            for _ in $(seq 30); do running || break; sleep 1; done
            running && kill "$(cat "$pidfile")" 2>/dev/null || true
            echo stopped
        else
            echo "not running"
        fi ;;
    fresh)
        running && die "power it off first: dev-vm.sh down"
        rm -f "$machine"
        echo "the next up starts from the base again" ;;
    *) sed -n '2,10p' "$0"; exit 2 ;;
esac
