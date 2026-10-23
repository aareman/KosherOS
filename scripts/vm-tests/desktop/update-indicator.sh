#!/usr/bin/env bash
# The "update ready" mark in the panel (#19), on the desktop machine with
# PR_USER signed in at seat0 (run by the desktop stage of `just pr-test`).
# With an update waiting the tray lists the mark, without one it does not;
# the person's own helper asks kosherd, as that person, which is the
# password-free path every account uses.
set -uo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
. "$here/../lib.sh"

u="${PR_USER:?PR_USER is the account signed in at the screen}"
. "$here/indicator.bash"

section "The update mark on the desktop"
desktop=$(systemctl --machine="$u@.host" --user show-environment 2>/dev/null \
    | sed -n 's/^XDG_CURRENT_DESKTOP=//p')
indicator_check "${desktop:-the desktop}"

report
