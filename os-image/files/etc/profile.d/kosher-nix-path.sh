# shellcheck shell=sh
# (sourced by every login shell, so it has no shebang of its own.)
# KosherOS: what is installed for an account from nixpkgs is on its PATH
# in a shell, whoever installed it (#191).
#
# kosherd installs an app for an account into the account's own profile,
# ~/.local/state/nix/profiles/profile. Nix's profile script (nix.sh) puts
# only ~/.nix-profile/bin on the PATH, and only once per environment: it
# sets __ETC_PROFILE_NIX_SOURCED and skips itself when that is already
# set. A shell started from one that had sourced it (su, runuser, a
# terminal some other program opened) inherits the marker and the PATH of
# whoever sourced it first, so the account's profile never made it in and
# an installed app was "command not found". This adds both directories,
# every time, after whatever is already there, and only once each. The
# session PATH (environment.d/60-kosher-nix-path.conf) names the same two.
if [ -n "${HOME:-}" ]; then
    for kosher_nix_bin in "$HOME/.nix-profile/bin" "$HOME/.local/state/nix/profiles/profile/bin"; do
        case ":${PATH:-}:" in
            *":$kosher_nix_bin:"*) ;;
            *) PATH="${PATH:+$PATH:}$kosher_nix_bin" ;;
        esac
    done
    unset kosher_nix_bin
    export PATH
fi
