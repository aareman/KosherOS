#!/usr/bin/env bash
# Build Umbriel and its portal from the two pinned checkouts into $out.
#
# Umbriel has no release tag and no Fedora package, so the image builds the
# commit it names. A commit hash is a checksum of the whole tree, and the
# checkout is refused unless HEAD is exactly that hash. Besides the files,
# $out/NEEDS lists the Fedora packages that own the shared libraries the
# two binaries link against, so the final stage installs exactly those and
# none of the build tools.
set -euo pipefail
umbriel=$1
portal=$2
out=$3
umbriel_ref=$4
portal_ref=$5

test "$(git -C "$umbriel" rev-parse HEAD)" = "$umbriel_ref"
test "$(git -C "$portal" rev-parse HEAD)" = "$portal_ref"

# The two projects spell "no tests" differently: a feature and a boolean.
meson setup "$umbriel/build" "$umbriel" --buildtype=release --prefix=/usr -Dtests=disabled
meson setup "$portal/build" "$portal" --buildtype=release --prefix=/usr -Dtests=false
for src in "$umbriel" "$portal"; do
    meson compile -C "$src/build"
    DESTDIR="$out" meson install -C "$src/build"
done

test -x "$out/usr/bin/umbriel"
test -x "$out/usr/bin/start-umbriel"
test -f "$out/usr/share/wayland-sessions/umbriel.desktop"
test -f "$out/usr/share/umbriel/config.toml"
grep -q '^Exec=start-umbriel' "$out/usr/share/wayland-sessions/umbriel.desktop"
test -f "$out/usr/share/xdg-desktop-portal/umbriel-portals.conf"

install -Dm644 "$umbriel/LICENSE" "$out/usr/share/licenses/umbriel/LICENSE"
install -Dm644 "$portal/LICENSE" "$out/usr/share/licenses/xdg-desktop-portal-umbriel/LICENSE"

# The packages behind every library the installed binaries need, and the X
# server Umbriel starts for X11 apps.
# (start-umbriel is a shell script, which ldd refuses; it is skipped.)
for exe in "$out"/usr/bin/* "$out"/usr/libexec/*; do
    ldd "$exe" 2>/dev/null || true
done | awk '/=> \//{print $3}' | sort -u \
    | xargs -r rpm -qf --qf '%{NAME}\n' | sort -u > "$out/NEEDS"
test -s "$out/NEEDS"
echo xorg-x11-server-Xwayland >> "$out/NEEDS"
cat "$out/NEEDS"
