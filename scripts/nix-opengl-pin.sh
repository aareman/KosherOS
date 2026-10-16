#!/usr/bin/env bash
# Pin the graphics drivers Nix apps use to a NixOS stable release's Mesa.
#   scripts/nix-opengl-pin.sh nixos-26.05
# Writes the store path into os-image/files/usr/share/kosher/nix-opengl-driver.env
# after checking cache.nixos.org has it, so a machine never has to build Mesa.
set -euo pipefail
branch=${1:?usage: $0 nixos-YY.MM}
case $branch in nixos-[0-9][0-9].[0-9][0-9]) ;; *) echo "a stable branch, like nixos-26.05" >&2; exit 2 ;; esac
file="$(dirname "$0")/../os-image/files/usr/share/kosher/nix-opengl-driver.env"
rev=$(git ls-remote https://github.com/NixOS/nixpkgs "refs/heads/$branch" | cut -f1)
[ -n "$rev" ] || { echo "no branch $branch" >&2; exit 1; }
path=$(nix eval --raw "github:NixOS/nixpkgs/$rev#mesa.outPath")
nix path-info --store https://cache.nixos.org "$path" >/dev/null \
    || { echo "$path is not on cache.nixos.org yet" >&2; exit 1; }
sed -i -e "s|^# nixos-.*|# $branch $rev|" -e "s|^KOSHER_OPENGL_DRIVER=.*|KOSHER_OPENGL_DRIVER=$path|" "$file"
echo "$branch $rev -> $path"
