#!/usr/bin/env bash
# Run the in-VM integration suites and summarise them.
#
#   run.sh              every suite
#   run.sh 20 50        only the suites whose names start 20/50
#   run.sh --list       what is available
#
# Runs as root INSIDE a test VM (see `just test-vm`, which pushes and calls
# this). Never point it at a machine you care about: it creates test users,
# changes filter modes, and toggles the guest account.
set -uo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
cd "$here" || exit 2

suites=()
for f in [0-9]*.sh; do suites+=("$f"); done

if [ "${1:-}" = "--list" ]; then
    printf '%s\n' "${suites[@]}"
    exit 0
fi

if [ $# -gt 0 ]; then
    wanted=()
    for prefix in "$@"; do
        for f in "${suites[@]}"; do
            case "$f" in "$prefix"*) wanted+=("$f") ;; esac
        done
    done
    suites=("${wanted[@]}")
    [ ${#suites[@]} -gt 0 ] || { echo "no suite matches: $*" >&2; exit 2; }
fi

[ "$(id -u)" = 0 ] || { echo "run me as root inside the test VM" >&2; exit 2; }

failed=()
started=$(date +%s)
for suite in "${suites[@]}"; do
    printf '\n\033[1;36m═══ %s ═══\033[0m\n' "$suite"
    bash "$suite" || failed+=("$suite")
done

printf '\n\033[1m══════════════════════════════════════════════════\033[0m\n'
printf 'Ran %d suite(s) in %ds\n' "${#suites[@]}" "$(( $(date +%s) - started ))"
if [ ${#failed[@]} -eq 0 ]; then
    printf '\033[32mAll suites passed\033[0m\n'
    exit 0
fi
printf '\033[31mFailed: %s\033[0m\n' "${failed[*]}"
exit 1
