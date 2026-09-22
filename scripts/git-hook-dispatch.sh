#!/bin/sh
# One pre-commit hook for this repository and every worktree of it.
#
# Git keeps a single hooks directory per repository ($GIT_COMMON_DIR/hooks)
# and every `git worktree` shares it. devenv's git-hooks integration
# installs a hook that names its own checkout's .pre-commit-config.yaml by
# absolute path, so in a repository with worktrees the last dev shell
# entered wins — and when that worktree is deleted the path goes with it
# and *every* commit in the repository fails with "config file not found".
#
# That is not hypothetical. This repository was found in exactly that
# state, pointing at a worktree called picture-filter that no longer
# existed, and Claude Code sessions make worktrees under .claude/worktrees/
# routinely.
#
# So this hook names no checkout at all. It asks git which working tree is
# being committed to and runs that tree's own configuration. Keep it that
# way: never bake a path into it.
#
# Installed by the dev shell (devenv.nix) and by `just hooks`.
set -eu

root=$(git rev-parse --show-toplevel 2>/dev/null) || exit 0
config="$root/.pre-commit-config.yaml"

# A branch older than these hooks, a fresh clone, or a checkout whose dev
# shell has never been entered has no configuration. That is not a failure:
# committing there must still work.
[ -f "$config" ] || exit 0

if ! command -v prek >/dev/null 2>&1; then
    echo "note: prek is not on PATH, so the commit checks were skipped." >&2
    echo "      Run them with 'just check', or enter the dev shell." >&2
    exit 0
fi

exec prek run --config "$config" --hook-stage pre-commit
