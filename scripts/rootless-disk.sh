#!/usr/bin/env bash
# A bootable qcow2 from a local image, as the ordinary user: no sudo.
#
#   rootless-disk.sh IMAGE OUT.qcow2
#
# bootc-image-builder (just vm) runs under rootful podman. This uses bcvk
# instead (github.com/bootc-dev/bcvk): it boots the image itself as a
# throwaway VM inside a rootless podman container and runs `bootc install
# to-disk` there, onto OUT. The only privilege it needs is /dev/kvm, which
# the kvm group gives.
#
# Two things bcvk takes from the host rather than from the dev shell,
# because its container chroots into the host's /usr: QEMU, and virtiofsd.
# Ubuntu 24.04's virtiofsd (1.10) is too old for the flags bcvk passes, so a
# static build of a current one is made with nix from the dev shell's own
# nixpkgs and put under /var/tmp, the one host directory the chroot sees.
#
# The disk holds no ssh key. The boot recipes hand root's key to the
# machine at each boot as a systemd credential (ssh.authorized_keys.root),
# which the image's tmpfiles turn into /root/.ssh/authorized_keys, so one
# disk serves any key. Next to OUT goes .image-id, the stamp boot-test.py
# checks so a stale disk is never tested as current.
set -euo pipefail

BCVK_VERSION=0.21.0
BCVK_SHA256=3135cc59b8971193b49be6cbaee12b789391ac6e6ab5ed3edf17082b03f144dd
BCVK_URL="https://github.com/bootc-dev/bcvk/releases/download/v${BCVK_VERSION}/bcvk-x86_64-unknown-linux-gnu.tar.gz"

image="${1:?usage: rootless-disk.sh IMAGE OUT.qcow2}"
out="${2:?usage: rootless-disk.sh IMAGE OUT.qcow2}"
repo="$(cd "$(dirname "$0")/.." && pwd)"
tools="$repo/build/tools"

die() { echo "rootless-disk: $*" >&2; exit 1; }

[ "$(id -u)" != 0 ] || die "run this as your own user, not root"
[ -w /dev/kvm ] || die "/dev/kvm is not writable; add yourself to the kvm group"
[ -x /usr/bin/qemu-system-x86_64 ] || [ -x /usr/libexec/qemu-kvm ] \
    || die "bcvk runs the host's QEMU from /usr; install qemu-system-x86 (Ubuntu) or qemu-kvm (Fedora)"
podman image exists "$image" || die "no local image $image; build it first (just build)"

bcvk_bin() {
    local dir="$tools/bcvk-$BCVK_VERSION"
    if [ ! -x "$dir/bcvk" ]; then
        mkdir -p "$dir"
        echo "fetching bcvk $BCVK_VERSION" >&2
        curl -fsSL -o "$dir/bcvk.tar.gz" "$BCVK_URL"
        echo "$BCVK_SHA256  $dir/bcvk.tar.gz" | sha256sum -c --quiet - \
            || { rm -f "$dir/bcvk.tar.gz"; die "bcvk download does not match its pinned checksum"; }
        tar -xzf "$dir/bcvk.tar.gz" -C "$dir"
        mv "$dir/bcvk-x86_64-unknown-linux-gnu" "$dir/bcvk"
        rm -f "$dir/bcvk.tar.gz"
    fi
    echo "$dir/bcvk"
}

# The nixpkgs revision the dev shell is pinned to, so this builds the same
# virtiofsd on every machine and CI.
virtiofsd_bin() {
    local dest rev store
    dest="/var/tmp/kosher-harness-$(id -u)/virtiofsd"
    if [ -x "$dest" ] && "$dest" --help 2>/dev/null | grep -q -- --allow-mmap; then
        echo "$dest"
        return
    fi
    command -v nix >/dev/null || die "nix is needed once, to build a static virtiofsd"
    rev="$(python3 -c 'import json,sys; n=json.load(open(sys.argv[1]))["nodes"]["nixpkgs"]["locked"]; print(n["owner"]+"/"+n["repo"]+"/"+n["rev"])' "$repo/devenv.lock")"
    echo "building a static virtiofsd from nixpkgs $rev (once)" >&2
    store="$(nix --extra-experimental-features 'nix-command flakes' build --no-link --print-out-paths "github:$rev#pkgsStatic.virtiofsd")"
    mkdir -p "$(dirname "$dest")"
    install -m755 "$store/bin/virtiofsd" "$dest"
    echo "$dest"
}

bcvk="$(bcvk_bin)"
virtiofsd="$(virtiofsd_bin)"

mkdir -p "$(dirname "$out")"
out="$(cd "$(dirname "$out")" && pwd)/$(basename "$out")"
rm -f "$out" "$(dirname "$out")/.image-id"

# 40 GiB, thin, like os-image/dev-config.toml asks of bootc-image-builder:
# an afternoon of Flatpaks and a Nix store fill the image's own size.
# ext4, as `just vm` makes it.
"$bcvk" to-disk --virtiofsd "$virtiofsd" --format qcow2 \
    --filesystem ext4 --disk-size 40G \
    "$image" "$out"

podman image inspect "$image" --format json \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)[0]["Id"])' \
    > "$(dirname "$out")/.image-id"
echo "qcow2 at $out (from $image)"
