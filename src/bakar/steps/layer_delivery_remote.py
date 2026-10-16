"""Remote half of layer delivery: the worktree script and its ownership marker.

The local half (``layer_delivery``) pushes the delivered commit to the remote
workspace's ``meta-avocado`` repository. This module renders the bash script
that runs on the remote host to place that commit in a detached worktree under
the worktree root, and the marker that records which dispatching host and
local checkout own the worktree.

The script only ever adds: it creates directories and lock files, adds a
worktree or checks out a new commit in one it already owns. It never deletes,
never prunes worktrees and never passes a force option, so a refusal leaves
the remote exactly as it found it.
"""

from __future__ import annotations

import json
import shlex
from datetime import UTC, datetime

from bakar.release_workspace import ReleaseRoutingError, is_valid_delivered_name

MARKER_FILENAME = "bakar-delivered.json"

EXIT_LOCKED = 3
EXIT_NO_MARKER = 4
EXIT_FOREIGN_MARKER = 5
EXIT_DIRTY = 6
EXIT_BUILD_RUNNING = 7

_MARKER_KEYS = ("source_host", "source_path", "name", "commit", "snapshot", "delivered_at")

_EXIT_DESCRIPTIONS = {
    EXIT_LOCKED: "another dispatch of the same name holds the delivery lock",
    EXIT_NO_MARKER: "the delivery path exists but carries no readable bakar marker",
    EXIT_FOREIGN_MARKER: "the delivered worktree belongs to another host or checkout",
    EXIT_DIRTY: "the delivered worktree has uncommitted changes",
    EXIT_BUILD_RUNNING: "a build is running in the delivered worktree's build directory",
}

# Compares the marker's owner against the expected host and path. Exit 4 when
# the marker cannot be read or parsed, 5 when it names another owner.
_MARKER_CHECK_PY = """\
import json, sys
path, host, src = sys.argv[1:4]
try:
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    got_host, got_src = data["source_host"], data["source_path"]
except (OSError, ValueError, KeyError, TypeError) as exc:
    print(f"bakar: cannot read marker {path}: {exc}", file=sys.stderr)
    sys.exit(4)
if (got_host, got_src) != (host, src):
    print(
        f"bakar: delivered worktree is owned by {got_host}:{got_src}, "
        f"not {host}:{src}",
        file=sys.stderr,
    )
    sys.exit(5)
"""


def marker_json(  # noqa: PLR0913 - one keyword-only argument per marker field
    *,
    source_host: str,
    source_path: str,
    name: str,
    commit: str,
    snapshot: bool,
    delivered_at: str | None = None,
) -> str:
    """Return the marker document recorded in a delivered worktree's git dir."""
    if delivered_at is None:
        delivered_at = datetime.now(UTC).isoformat(timespec="seconds")
    payload = {
        "source_host": source_host,
        "source_path": source_path,
        "name": name,
        "commit": commit,
        "snapshot": snapshot,
        "delivered_at": delivered_at,
    }
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def parse_marker(text: str) -> dict[str, str | bool]:
    """Parse a marker document, raising ``ValueError`` when it is malformed."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"marker is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("marker is not a JSON object")  # noqa: TRY004
    missing = [key for key in _MARKER_KEYS if key not in data]
    if missing:
        raise ValueError(f"marker is missing {', '.join(missing)}")
    for key in _MARKER_KEYS:
        expected = bool if key == "snapshot" else str
        if not isinstance(data[key], expected):
            raise ValueError(f"marker field {key} is not a {expected.__name__}")  # noqa: TRY004
    return {key: data[key] for key in _MARKER_KEYS}


def describe_exit(code: int) -> str:
    """Return a human-readable reason for an exit status of the worktree script."""
    if code == 0:
        return "delivered"
    return _EXIT_DESCRIPTIONS.get(code, f"the delivery script failed with exit status {code}")


def ensure_worktree_script(  # noqa: PLR0913 - every value is keyword-only and lands in a distinct script variable
    *,
    workspace: str,
    worktree_root: str,
    name: str,
    commit: str,
    snapshot: bool,
    source_host: str,
    source_path: str,
    build_dir: str,
) -> str:
    """Return the bash script that places ``commit`` at ``<worktree_root>/<name>``.

    Exit statuses: 0 delivered or already current, 3 lock held, 4 no readable
    marker, 5 marker from another owner, 6 dirty tree, 7 build running.
    """
    if not is_valid_delivered_name(name):
        raise ReleaseRoutingError(
            f"delivered name {name!r} must match [A-Za-z0-9._-]+",
        )
    q = shlex.quote
    marker = marker_json(
        source_host=source_host,
        source_path=source_path,
        name=name,
        commit=commit,
        snapshot=snapshot,
    )
    return f"""\
set -euo pipefail
workspace={q(workspace)}
root={q(worktree_root)}
name={q(name)}
commit={q(commit)}
src_host={q(source_host)}
src_path={q(source_path)}
build_lock="$workspace"/{q(build_dir)}/build/bitbake.lock
marker={q(marker)}
dest="$root/$name"

write_marker() {{
    local gitdir
    gitdir=$(git -C "$dest" rev-parse --absolute-git-dir)
    printf '%s' "$marker" > "$gitdir/{MARKER_FILENAME}"
}}

mkdir -p -- "$root"
exec 9>>"$root/.$name.lock"
if ! flock -n 9; then
    echo "bakar: another dispatch of $name holds $root/.$name.lock" >&2
    exit {EXIT_LOCKED}
fi

if [ ! -e "$dest" ]; then
    git -C "$workspace/meta-avocado" worktree add -q --detach "$dest" "$commit"
    write_marker
    echo "bakar: delivered $commit to $dest"
    exit 0
fi

if [ ! -e "$dest/.git" ]; then
    echo "bakar: $dest exists but is not a delivered worktree (no $dest/.git)" >&2
    exit {EXIT_NO_MARKER}
fi
gitdir=$(git -C "$dest" rev-parse --absolute-git-dir) || {{
    echo "bakar: $dest exists but its git directory cannot be read" >&2
    exit {EXIT_NO_MARKER}
}}
marker_path="$gitdir/{MARKER_FILENAME}"
if [ ! -r "$marker_path" ]; then
    echo "bakar: $dest has no readable marker at $marker_path" >&2
    exit {EXIT_NO_MARKER}
fi
python3 -c {q(_MARKER_CHECK_PY)} "$marker_path" "$src_host" "$src_path"

head=$(git -C "$dest" rev-parse HEAD)
want=$(git -C "$dest" rev-parse --verify "$commit^{{commit}}")
if [ "$head" = "$want" ]; then
    echo "bakar: $dest already at $commit"
    exit 0
fi

status=$(git -C "$dest" status --porcelain)
if [ -n "$status" ]; then
    echo "bakar: $dest has uncommitted changes:" >&2
    printf '%s\\n' "$status" >&2
    exit {EXIT_DIRTY}
fi

if [ -e "$build_lock" ] && ! flock -n -s "$build_lock" true; then
    echo "bakar: a build holds $build_lock" >&2
    exit {EXIT_BUILD_RUNNING}
fi

git -C "$dest" checkout -q --detach "$commit"
write_marker
echo "bakar: moved $dest to $commit"
"""
