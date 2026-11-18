# Anaconda's interactive defaults for the KosherOS web installer
# (os-image/installer/Containerfile). Nothing here answers the disk
# question: with no partitioning, the person picks the disk and confirms
# what will be erased (#134).
#
# The payload: KosherOS from the ISO itself; the installed machine
# follows @TARGET@ for updates.
bootc --source-imgref oci:/usr/share/kosher/os-image --target-imgref @TARGET@
timezone America/New_York --utc
keyboard us
lang en_US.UTF-8

%pre --interpreter=/bin/bash --log=/tmp/kosher-pre.log
# Pick the disk when there is only one to pick.
set -u
out=/tmp/kosher-disk.ks
: > "$out"

# The device the installer itself is running from must never be a target.
media=""
src=$(findmnt -n -o SOURCE /run/install/repo 2>/dev/null || true)
if [ -n "$src" ]; then
    media=$(lsblk -n -o PKNAME "$src" 2>/dev/null | head -1)
    [ -n "$media" ] || media=$(basename "$src")
fi

candidates=()
for path in /sys/block/*; do
    name=$(basename "$path")
    case "$name" in
        loop*|zram*|ram*|sr*|dm-*|md*) continue ;;
    esac
    [ "$(cat "$path/removable" 2>/dev/null)" = "0" ] || continue
    [ "$name" = "$media" ] && continue
    size=$(cat "$path/size" 2>/dev/null || echo 0)   # 512-byte sectors
    [ "$size" -ge 41943040 ] || continue              # at least 20 GiB
    candidates+=("$name")
done

echo "install medium: ${media:-unknown}; candidate disks: ${candidates[*]:-none}"
if [ "${#candidates[@]}" -eq 1 ]; then
    disk="${candidates[0]}"
    # Only the disk. No clearpart, no autopart: with those the kickstart is
    # complete and Anaconda installs without a click (#134).
    echo "ignoredisk --only-use=$disk" > "$out"
    echo "one internal disk ($disk): shown as the destination; the person still confirms"
else
    echo "not exactly one internal disk: Anaconda will ask"
fi
%end
%include /tmp/kosher-disk.ks
