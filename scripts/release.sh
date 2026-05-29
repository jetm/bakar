#!/usr/bin/env bash
#
# release.sh - Atomic bakar release driver.
#
# Enforces clean-tree, branch, sync, and changelog preconditions; runs the
# local validation suite; then bumps the version and pushes commit + tag in
# one atomic step. No interactive prompt or sleep is permitted between the
# bump and the push - that window is precisely the failure mode (stale
# README on PyPI in v0.0.3) this script exists to eliminate.
#
# Auto-generates [Unreleased] changelog entries via `devtool changelog --write`
# when the section is empty, and auto-pushes local commits that have not yet
# been pushed to origin/main.

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
patch | minor | major) ;;
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

# (b) Working tree must be clean (no staged, unstaged, or untracked changes).
if [ -n "$(git status --porcelain)" ]; then
	die "working tree has uncommitted changes; commit or stash before releasing"
fi

# (c) Current branch must be main.
CURRENT_BRANCH="$(git rev-parse --abbrev-ref HEAD)"
if [ "$CURRENT_BRANCH" != "main" ]; then
	die "current branch is '$CURRENT_BRANCH'; releases must be cut from main"
fi

# (d) Sync check: local must not be behind or diverged from origin/main.
#     Being ahead is fine - those commits will be pushed atomically at the end
#     together with the version bump commit and tag.
if ! git fetch origin main; then
	die "git fetch origin main failed; check network and remote access"
fi

LOCAL_MAIN="$(git rev-parse main)"
REMOTE_MAIN="$(git rev-parse origin/main)"
if [ "$LOCAL_MAIN" != "$REMOTE_MAIN" ]; then
	AHEAD="$(git rev-list --count origin/main..main)"
	BEHIND="$(git rev-list --count main..origin/main)"
	if [ "$BEHIND" -gt 0 ] && [ "$AHEAD" -eq 0 ]; then
		die "local main is behind origin/main by $BEHIND commit(s); pull first"
	elif [ "$AHEAD" -gt 0 ] && [ "$BEHIND" -gt 0 ]; then
		die "local main has diverged from origin/main (ahead $AHEAD, behind $BEHIND); resolve before releasing"
	fi
	# AHEAD > 0, BEHIND == 0: will be pushed atomically at the end.
	echo "release.sh: local main is $AHEAD commit(s) ahead of origin/main; will push at end"
fi

# (e) CHANGELOG.md must have non-empty ## [Unreleased] content.
#     If the section is empty, auto-generate via `devtool changelog --write`.
#     The generated entry is staged but not committed here; bump-my-version
#     picks it up and includes it in the version bump commit.
check_unreleased() {
	awk '
        /^## \[Unreleased\]/ { in_block = 1; next }
        in_block && /^## \[/ { exit }
        in_block && $0 !~ /^[[:space:]]*$/ && $0 !~ /^[[:space:]]*#/ { print }
    ' CHANGELOG.md
}

UNRELEASED_BODY="$(check_unreleased)"
if [ -z "$UNRELEASED_BODY" ]; then
	echo "release.sh: [Unreleased] is empty; running devtool changelog --write ..."
	if ! devtool changelog --write --cwd "$REPO_ROOT"; then
		die "devtool changelog --write failed; fill [Unreleased] manually before releasing"
	fi
	# Stage so bump-my-version includes the generated entry in the bump commit.
	git add CHANGELOG.md
	# Re-validate: devtool changelog might find no commits since the last tag.
	UNRELEASED_BODY="$(check_unreleased)"
	if [ -z "$UNRELEASED_BODY" ]; then
		die "devtool changelog produced no entries; fill [Unreleased] manually before releasing"
	fi
	echo "release.sh: changelog entry generated and staged"
fi

# (f) Validation suite. Each step exits non-zero on failure; set -e propagates.
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

echo "==> uv sync --frozen"
uv sync --frozen

# All gates passed.
# Bump and push atomically. NO prompt, sleep, or user-interaction step is
# permitted between these two commands - that window is the v0.0.3 failure
# mode this script exists to prevent.
echo "==> uv run bump-my-version bump $BUMP_TYPE"
TERM=dumb uv run bump-my-version bump "$BUMP_TYPE"
echo "==> git push origin main --follow-tags"
git push origin main --follow-tags

echo "release.sh: $BUMP_TYPE bump pushed; publish workflow should pick up the tag shortly"
