#!/usr/bin/env bash
# Manage the stage-1 Fedora test VM: plain QEMU/KVM + cloud-init, no libvirt.
# All tools (qemu-system-x86_64, cloud-localds) come from the devenv shell.
#
#   fedora-vm.sh up       fetch image (once), create VM (once), boot headless
#   fedora-vm.sh ssh      shell into it (also: ssh -F build/vm/ssh_config kosher-fedora)
#   fedora-vm.sh status
#   fedora-vm.sh down     power off
#   fedora-vm.sh destroy  delete the VM disk (base image is kept)
set -euo pipefail

FEDORA_RELEASE="${FEDORA_RELEASE:-44}"
IMAGE_NAME="Fedora-Cloud-Base-Generic-${FEDORA_RELEASE}-1.1.x86_64.qcow2"
MIRRORS=(
    "https://download.fedoraproject.org/pub/fedora/linux/releases/${FEDORA_RELEASE}/Cloud/x86_64/images/${IMAGE_NAME}"
    "https://archives.fedoraproject.org/pub/archive/fedora/linux/releases/${FEDORA_RELEASE}/Cloud/x86_64/images/${IMAGE_NAME}"
)

repo="$(cd "$(dirname "$0")/.." && pwd)"
vm_dir="$repo/build/vm"
base_img="$vm_dir/$IMAGE_NAME"
disk="$vm_dir/disk.qcow2"
seed="$vm_dir/seed.iso"
pidfile="$vm_dir/qemu.pid"
ssh_config="$vm_dir/ssh_config"
ssh_port="${KOSHER_VM_SSH_PORT:-2222}"

mkdir -p "$vm_dir"

ssh_pubkey() {
    for k in ~/.ssh/id_ed25519.pub ~/.ssh/id_rsa.pub; do
        [ -f "$k" ] && { cat "$k"; return; }
    done
    [ -f "$vm_dir/id_ed25519" ] || ssh-keygen -t ed25519 -N "" -f "$vm_dir/id_ed25519" -C kosher-vm >/dev/null
    cat "$vm_dir/id_ed25519.pub"
}

identity_file() {
    for k in ~/.ssh/id_ed25519 ~/.ssh/id_rsa; do
        [ -f "$k.pub" ] && { echo "$k"; return; }
    done
    echo "$vm_dir/id_ed25519"
}

fetch_base() {
    [ -f "$base_img" ] && return
    for url in "${MIRRORS[@]}"; do
        echo "fetching $url"
        if curl -fL --progress-bar -o "$base_img.part" "$url"; then
            mv "$base_img.part" "$base_img"
            return
        fi
    done
    echo "could not download Fedora ${FEDORA_RELEASE} cloud image; check the release name in ${MIRRORS[0]}" >&2
    exit 1
}

create_vm() {
    [ -f "$disk" ] && return
    # Overlay disk: the pristine base image is never modified; destroy+up = fresh VM.
    qemu-img create -q -f qcow2 -b "$base_img" -F qcow2 "$disk" 30G

    cat > "$vm_dir/user-data" <<EOF
#cloud-config
hostname: kosher-fedora
users:
  - name: fedora
    groups: wheel
    sudo: ALL=(ALL) NOPASSWD:ALL
    ssh_authorized_keys:
      - $(ssh_pubkey)
EOF
    printf 'instance-id: kosher-fedora\nlocal-hostname: kosher-fedora\n' > "$vm_dir/meta-data"
    cloud-localds "$seed" "$vm_dir/user-data" "$vm_dir/meta-data"

    write_ssh_config
}

write_ssh_config() {
    cat > "$ssh_config" <<EOF
Host kosher-fedora
    HostName 127.0.0.1
    Port $ssh_port
    User fedora
    IdentityFile $(identity_file)
    StrictHostKeyChecking no
    UserKnownHostsFile /dev/null
    LogLevel ERROR

# The GUI qcow2 VM (just boot-image), when built from os-image/dev-config.toml
Host kosher-gui
    HostName 127.0.0.1
    Port 2223
    User root
    IdentityFile $(identity_file)
    StrictHostKeyChecking no
    UserKnownHostsFile /dev/null
    LogLevel ERROR
EOF
}

running() {
    [ -f "$pidfile" ] && kill -0 "$(cat "$pidfile")" 2>/dev/null
}

cmd_up() {
    if running; then
        echo "already running (pid $(cat "$pidfile"))"
        return
    fi
    # An existing VM disk keeps whatever release it was created from.
    [ -f "$disk" ] || fetch_base
    create_vm
    qemu-system-x86_64 \
        -enable-kvm -cpu host -m 4096 -smp 4 \
        -drive file="$disk",if=virtio \
        -drive file="$seed",if=virtio,format=raw,readonly=on \
        -netdev user,id=n0,hostfwd=tcp:127.0.0.1:"${ssh_port}"-:22 \
        -device virtio-net-pci,netdev=n0 \
        -display none -serial "file:$vm_dir/console.log" \
        -daemonize -pidfile "$pidfile"
    echo -n "booting"
    for _ in $(seq 60); do
        if ssh -F "$ssh_config" -o ConnectTimeout=2 kosher-fedora true 2>/dev/null; then
            echo; echo "up — connect with: just fedora-ssh"
            return
        fi
        echo -n "."; sleep 2
    done
    echo; echo "VM did not become reachable; see $vm_dir/console.log" >&2
    exit 1
}

case "${1:-}" in
    up) cmd_up ;;
    ssh) shift; exec ssh -F "$ssh_config" kosher-fedora "$@" ;;
    status) running && echo "running (pid $(cat "$pidfile"))" || echo "stopped" ;;
    down)
        if running && kill "$(cat "$pidfile")"; then
            echo stopped
        else
            echo "not running"
        fi ;;
    destroy)
        if running; then
            kill "$(cat "$pidfile")" 2>/dev/null || true
        fi
        rm -f "$disk" "$seed" "$vm_dir/user-data" "$vm_dir/meta-data" "$pidfile"
        echo "VM deleted (base image kept at $base_img)" ;;
    *) grep '^#   ' "$0" | sed 's/^#   //'; exit 1 ;;
esac
