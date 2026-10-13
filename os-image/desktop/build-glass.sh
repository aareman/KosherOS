#!/usr/bin/env bash
# Build the Aura Glass shell preset offline from the two pinned checkouts.
# No user installer, session settings, or files in a real home are touched.
set -euo pipefail
aura=$1
bms=$2
out=$3
for patch in blur-my-shell-overview.patch blur-my-shell-subwindows.patch blur-my-shell-notifications.patch; do
    git -C "$bms" apply --whitespace=nowarn "$aura/patches/$patch"
done
make -C "$bms" build
extension="$out/usr/share/gnome-shell/extensions/blur-my-shell@aunetx"
mkdir -p "$extension"
unzip -q "$bms/build/blur-my-shell@aunetx.shell-extension.zip" -d "$extension"
glib-compile-schemas --strict "$extension/schemas"
python3 - "$extension/metadata.json" <<'PY'
import json, sys
assert '50' in json.load(open(sys.argv[1]))['shell-version']
PY

theme="$out/usr/share/themes/KosherOS-Aura/gnome-shell"
mkdir -p "$theme"
# Retain GNOME's complete base theme and layer Aura's shell surfaces over it.
# GTK apps keep their own light/dark appearance and accessibility settings.
printf '@import url("resource:///org/gnome/shell/theme/gnome-shell-dark.css");\n' > "$theme/gnome-shell.css"
for sheet in shell-10-quick-settings.css shell-20-popup-menus.css shell-30-notifications.css shell-40-quick-toggles.css shell-50-dialogs.css; do
    cat "$aura/css/$sheet" >> "$theme/gnome-shell.css"
done
cat "$(dirname "$0")/kosher-glass.css" >> "$theme/gnome-shell.css"
cp "$aura/LICENSE" "$theme/../LICENSE-Aura-Glass"
# Keep the exact patch sources and provenance with the distributed extension.
mkdir -p "$out/usr/share/doc/kosher-glass/patches"
cp "$aura"/patches/blur-my-shell-*.patch "$out/usr/share/doc/kosher-glass/patches/"
printf 'Aura Glass: %s\nBlur My Shell: %s\n' \
    "$(git -C "$aura" rev-parse HEAD)" "$(git -C "$bms" rev-parse HEAD)" \
    > "$out/usr/share/doc/kosher-glass/versions"
