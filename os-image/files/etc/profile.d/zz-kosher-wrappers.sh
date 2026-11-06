# shellcheck shell=sh
# (sourced by every login shell, so it has no shebang of its own.)
# KosherOS: /run/wrappers/bin first on PATH, as on NixOS. kosherd keeps
# setgid copies of programs there that only work setgid, such as 1Password's
# `op` (#234, kosherd/onepassword.py); the same program from a person's Nix
# profile, later on PATH, is not setgid and the 1Password app would not
# trust it. Only root writes the directory. Named to run after nix.sh,
# which puts the Nix profile at the front of PATH.
case ":${PATH:-}:" in
    *":/run/wrappers/bin:"*) ;;
    *) PATH="/run/wrappers/bin${PATH:+:$PATH}" ;;
esac
export PATH
