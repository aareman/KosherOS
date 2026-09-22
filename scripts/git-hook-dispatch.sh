#!/bin/sh
# One git hook for this repository and every worktree of it, installed under
# several names — pre-commit, pre-push, whichever stages the dev shell sets
# up — and told which it is by $0.
#
# Git keeps a single hooks directory per repository ($GIT_COMMON_DIR/hooks)
# and every `git worktree` shares it. devenv's git-hooks integration
# installs, for each stage, a shim that names its own checkout's
# .pre-commit-config.yaml by absolute path. So in a repository with
# worktrees the last dev shell entered wins, and when that worktree is
# deleted — or a shell is entered on a branch that has no hooks, which
# removes the generated config — the path goes with it and every commit
# *and every push* in the repository fails with "config file not found".
#
# That is not hypothetical. This repository was found in that state,
# pointing at a worktree called picture-filter that no longer existed; and
# while this file was being written the pre-push shim did the same thing
# within the hour. Claude Code sessions make worktrees under
# .claude/worktrees/ routinely.
#
# So this hook names no checkout at all. It asks git which working tree is
# being committed to or pushed from and runs that tree's own configuration.
# Keep it that way: never bake a path into it.
#
# Installed by the dev shell (devenv.nix) and by `just hooks`.
set -eu

stage=$(basename "$0")
here=$(cd "$(dirname "$0")" && pwd)
root=$(git rev-parse --show-toplevel 2>/dev/null) || exit 0
config="$root/.pre-commit-config.yaml"

# A branch older than these hooks, a fresh clone, or a checkout whose dev
# shell has never been entered has no configuration. That is not a failure:
# committing and pushing there must still work.
[ -f "$config" ] || exit 0

if ! command -v prek >/dev/null 2>&1; then
    echo "note: prek is not on PATH, so the $stage checks were skipped." >&2
    echo "      Run them with 'just check', or enter the dev shell." >&2
    exit 0
fi

# prek's own shim, minus the baked-in path. hook-impl knows what git hands
# each stage (pre-push reads the pushed refs from stdin, for instance),
# which a plain `prek run` does not.
exec prek hook-impl --hook-dir "$here" --hook-type="$stage" \
    --skip-on-missing-config --config="$config" -- "$@"
