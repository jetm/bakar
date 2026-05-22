#!/usr/bin/env bash
#
# release.sh - Atomic bspctl release driver.
#
# Enforces clean-tree, branch, sync, and changelog preconditions; runs the
# local validation suite; then bumps the version and pushes commit + tag in
# one atomic step. No interactive prompt or sleep is permitted between the
# bump and the push - that window is precisely the failure mode (stale
# README on PyPI in v0.0.3) this script exists to eliminate.

set -euo pipefail

usage() {
    cat >&2 <<'EOF'
Usage: scripts/release.sh <patch|minor|major>

Runs preconditions and validations, then bumps and pushes atomically.
EOF
}

die() {
    printf 'release.sh: %s\n' "$1" >&2
    exit 1
}

# (a) Exactly one positional argument in {patch, minor, major}.
if [ "$#" -ne 1 ]; then
    usage
    exit 1
fi

case "$1" in
    patch|minor|major) ;;
    *)
        printf 'release.sh: invalid bump type %q; must be one of patch, minor, major\n' "$1" >&2
        usage
        exit 1
        ;;
esac

BUMP_TYPE="$1"

# Move to the repo root so all subsequent commands are path-independent.
REPO_ROOT="$(git rev-parse --show-toplevel)"
cd "$REPO_ROOT"

# (b) Current branch must be main.
CURRENT_BRANCH="$(git rev-parse --abbrev-ref HEAD)"
if [ "$CURRENT_BRANCH" != "main" ]; then
    die "current branch is '$CURRENT_BRANCH'; releases must be cut from main"
fi

# (c) Working tree must be clean (no staged, unstaged, or untracked changes).
if [ -n "$(git status --porcelain)" ]; then
    die "working tree has uncommitted changes; commit or stash before releasing"
fi

# (d) Local main must match origin/main exactly (no divergence in either
#     direction). Fetch first so the comparison sees current remote state.
if ! git fetch origin main; then
    die "git fetch origin main failed; check network and remote access"
fi

LOCAL_MAIN="$(git rev-parse main)"
REMOTE_MAIN="$(git rev-parse origin/main)"
if [ "$LOCAL_MAIN" != "$REMOTE_MAIN" ]; then
    # Distinguish ahead vs behind for an actionable message.
    AHEAD="$(git rev-list --count origin/main..main)"
    BEHIND="$(git rev-list --count main..origin/main)"
    if [ "$AHEAD" -gt 0 ] && [ "$BEHIND" -eq 0 ]; then
        die "local main is ahead of origin/main by $AHEAD commit(s); push first"
    elif [ "$BEHIND" -gt 0 ] && [ "$AHEAD" -eq 0 ]; then
        die "local main is behind origin/main by $BEHIND commit(s); pull first"
    else
        die "local main has diverged from origin/main (ahead $AHEAD, behind $BEHIND)"
    fi
fi

# (e) CHANGELOG.md must have non-empty `## [Unreleased]` content.
#     Pull lines between `## [Unreleased]` (exclusive) and the next `## [`
#     (exclusive), drop blank lines and `### ...` subheadings; require >=1
#     remaining line.
UNRELEASED_BODY="$(
    awk '
        /^## \[Unreleased\]/ { in_block = 1; next }
        in_block && /^## \[/ { exit }
        in_block { print }
    ' CHANGELOG.md \
    | grep -vE '^\s*$' \
    | grep -vE '^###' \
    || true
)"
if [ -z "$UNRELEASED_BODY" ]; then
    die "CHANGELOG.md has no entries under [Unreleased]; fill the section before releasing"
fi

# (f) Validation suite. Each step exits non-zero on failure; set -e takes
#     over from there. Print a header before each so the failure point is
#     obvious in the terminal.
echo "==> uv run pytest"
uv run pytest

echo "==> uv run ruff check src/ tests/"
uv run ruff check src/ tests/

echo "==> uv run ruff format --check src/ tests/"
uv run ruff format --check src/ tests/

echo "==> uv run ty check src/"
uv run ty check src/

echo "==> uv build"
uv build

echo "==> uvx twine check dist/*"
uvx twine check dist/*

# All gates passed. Bump and push atomically. NO prompt, sleep, or
# user-interaction step is permitted between these two commands - that
# window is the v0.0.3 failure mode we exist to prevent.
echo "==> uv run bump-my-version bump $BUMP_TYPE"
uv run bump-my-version bump "$BUMP_TYPE"
echo "==> git push origin main --follow-tags"
git push origin main --follow-tags

echo "release.sh: $BUMP_TYPE bump pushed; publish workflow should pick up the tag shortly"
