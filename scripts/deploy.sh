#!/usr/bin/env bash
# Everything this checkout changes, into a running VM, in seconds:
# kosherd, the apps, os-image/files (/etc and /usr) and the policy schema,
# then the daemon and the filter restarted.
#
#   deploy.sh [VM]     a host in build/vm/ssh_config (kosher-dev by default)
#
# On a bootc machine /usr is read-only; `bootc usr-overlay` makes it
# writable until the next reboot, which is a dev push's lifetime. Packages
# go where Python imports them from, mirrored (a file removed here is
# removed there); /etc and /usr files are copied over what is there. What
# this cannot carry is a change to the Containerfile itself — a package, a
# build step: `just build && just vm-upgrade` for that.
set -euo pipefail

repo="$(cd "$(dirname "$0")/.." && pwd)"
vm="${1:-kosher-dev}"
vmssh=(ssh -F "$repo/build/vm/ssh_config")
stage=/tmp/kosher-deploy

# Package directory -> import name; what pip installed from each app.
packages=(
    "kosherd/src/kosherd kosherd"
    "admin-app/src/kosheradmin kosheradmin"
    "store-app/src/kosherstore kosherstore"
    "setup-app/src/koshersetup koshersetup"
    "myfilter-app/src/koshermyfilter koshermyfilter"
    "welcome-app/src/kosherwelcome kosherwelcome"
    "look-app/src/kosherlook kosherlook"
    "search-app/kosher_search kosher_search"
)

"${vmssh[@]}" "$vm" "rm -rf $stage && mkdir -p $stage/pkg $stage/files"
for entry in "${packages[@]}"; do
    read -r dir name <<< "$entry"
    rsync -a --delete --exclude __pycache__ -e "${vmssh[*]}" "$repo/$dir/" "$vm:$stage/pkg/$name/"
done
rsync -a --exclude __pycache__ -e "${vmssh[*]}" "$repo/os-image/files/" "$vm:$stage/files/"
rsync -a -e "${vmssh[*]}" "$repo/policy/schema/policy.schema.json" "$vm:$stage/policy.schema.json"
# What the Containerfile copies from outside os-image/files: the proxy's
# addons and scripts, and the branding.
rsync -a --exclude __pycache__ -e "${vmssh[*]}" "$repo/mitm/" "$vm:$stage/mitm/"
rsync -a -e "${vmssh[*]}" "$repo/branding/" "$vm:$stage/branding/"

"${vmssh[@]}" "$vm" bash -s "$stage" <<'EOF'
set -euo pipefail
stage="$1"
S=$([ "$(id -u)" = 0 ] || echo sudo)
$S bootc usr-overlay >/dev/null 2>&1 || true
for d in "$stage"/pkg/*/; do
    name=$(basename "$d")
    dest=$($S python3 -c "import $name, os; print(os.path.dirname($name.__file__))" 2>/dev/null) \
        || { echo "  $name: not installed here, skipped"; continue; }
    $S rsync -a --delete --exclude __pycache__ "$d" "$dest/"
    echo "  $name -> $dest"
done
$S rsync -a "$stage/files/usr/" /usr/
$S rsync -a "$stage/files/etc/" /etc/
$S install -m644 "$stage/policy.schema.json" /usr/share/kosher/policy.schema.json
$S rsync -a "$stage/mitm/" /usr/share/kosher/mitm/
$S rsync -a "$stage/branding/" /usr/share/kosher/branding/
$S rm -rf "$stage"
$S systemctl daemon-reload
$S systemctl restart kosherd kosher-mitm kosher-search 2>&1 || true
for unit in kosherd kosher-dns kosher-mitm kosher-search; do
    printf '  %-14s %s\n' "$unit" "$($S systemctl is-active "$unit" 2>&1)"
done
EOF
