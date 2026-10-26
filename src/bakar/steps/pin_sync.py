"""Move the shared clones of a release workspace onto their pins.

The script this module renders runs on whichever machine owns the workspace.
It changes nothing but ``HEAD`` and the working tree of a clone that is clean
and off its pin: no branch, ref, config or file outside the clone is touched,
and no force, reset, clean or stash is used. A dirty clone, a missing clone and
a side checkout are left alone. Both workspace locks are taken before any
clone moves, and a build that uses the workspace refuses the whole run.

Entries are identified by their index in the caller's list and never by name in
the script's output, so a hostile name cannot forge a line.
"""

from __future__ import annotations

import re
import shlex
import subprocess
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from bakar.steps import remote_dispatch

if TYPE_CHECKING:
    from pathlib import Path

    from bakar.steps.repo_reuse import PinnedRepo

EXIT_REFUSED = 3
SYNC_TIMEOUT_SECONDS = 3600.0

_FULL_PIN_RE = re.compile(r"[0-9a-fA-F]{40}")


def lock_preamble(workspace: Path) -> str:
    """Bash taking the in-use and repos locks and probing every build directory.

    Both locks stay held on fds 7 and 9 until the script exits; every child
    started afterwards closes them so a git hook cannot outlive the script
    holding them. A lock file that merely exists refuses nothing: only a live
    ``flock`` holder does.
    """
    ws = shlex.quote(str(workspace))
    return f"""workspace={ws}
mkdir -p -- "$workspace/.bakar"
exec 7>>"$workspace/.bakar/repos-in-use.lock"
if ! flock -n -x 7; then
    printf 'BAKAR_SYNC_REFUSED\\t%s\\n' "a build or doctor holds $workspace/.bakar/repos-in-use.lock"
    exit {EXIT_REFUSED}
fi
exec 9>>"$workspace/.bakar/repos.lock"
if ! flock -n -x 9; then
    printf 'BAKAR_SYNC_REFUSED\\t%s\\n' "another run holds $workspace/.bakar/repos.lock"
    exit {EXIT_REFUSED}
fi
for build_lock in "$workspace"/*/build/bitbake.lock; do
    if [ -e "$build_lock" ] && ! flock -n -s "$build_lock" true 7>&- 9>&-; then
        dir=${{build_lock%/build/bitbake.lock}}
        printf 'BAKAR_SYNC_REFUSED\\t%s\\n' "a build holds $build_lock (build directory $dir)"
        exit {EXIT_REFUSED}
    fi
done
"""


def validate_sync_entries(entries: list[PinnedRepo]) -> dict[int, str]:
    """Return index -> reason for each pinned entry that must not be embedded."""
    rejected: dict[int, str] = {}
    for index, entry in enumerate(entries):
        if entry.pin is None:
            continue
        if not _FULL_PIN_RE.fullmatch(entry.pin):
            rejected[index] = "pin is not a full 40-character commit id"
        elif entry.url.startswith("-"):
            rejected[index] = "url begins with '-'"
        elif any(c in value for value in (entry.name, str(entry.path)) for c in "\t\n"):
            rejected[index] = "name or path contains a tab or newline"
    return rejected


_SYNC_FUNCTION = r"""
examined=0
revert=""

row() {
    printf 'BAKAR_SYNC\t%s\t%s\t%s\t%s\t%s\n' "$1" "$2" "$3" "$4" "$5"
}

sync_one() {
    local idx=$1 name=$2 path=$3 url=$4 pin=$5
    local head branch st err now rc
    examined=$((examined + 1))
    if [ ! -d "$path" ]; then
        row "$idx" MISSING - - -
        return
    fi
    head=$(git -C "$path" rev-parse HEAD 2>/dev/null 7>&- 9>&-) || {
        row "$idx" FAIL - - "cannot read HEAD"
        return
    }
    branch=$(git -C "$path" symbolic-ref -q --short HEAD 2>/dev/null 7>&- 9>&-) || branch=-
    [ -n "$branch" ] || branch=-
    case "$head" in
        "$pin"*)
            row "$idx" AT_PIN "$head" "$branch" -
            return
            ;;
    esac
    st=$(git -C "$path" status --porcelain 2>&1 7>&- 9>&-) || {
        row "$idx" FAIL "$head" "$branch" "cannot read status"
        return
    }
    if [ -n "$st" ]; then
        row "$idx" DIRTY "$head" "$branch" -
        return
    fi
    if ! git -C "$path" cat-file -e "$pin^{commit}" 2>/dev/null 7>&- 9>&-; then
        if ! err=$(git -C "$path" fetch --no-tags -q --end-of-options "$url" "$pin" 2>&1 7>&- 9>&-); then
            row "$idx" FAIL "$head" "$branch" "fetch failed: $(printf '%s' "$err" | tr '\t\n' '  ')"
            return
        fi
    fi
    if [ -z "$revert" ]; then
        local stamp
        stamp=$(date +%Y%m%dT%H%M%S)
        mkdir -p -- "$workspace/.bakar/sync-revert" &&
            : >> "$workspace/.bakar/sync-revert/$stamp-$$.tsv" || {
            row "$idx" FAIL "$head" "$branch" "cannot create the revert record"
            return
        }
        revert="$workspace/.bakar/sync-revert/$stamp-$$.tsv"
        printf 'BAKAR_SYNC_REVERT\t%s\n' "$revert"
    fi
    printf '%s\t%s\t%s\t%s\n' "$name" "$path" "$head" "$branch" >> "$revert" || {
        row "$idx" FAIL "$head" "$branch" "cannot write the revert record"
        return
    }
    if ! err=$(git -C "$path" checkout -q --detach "$pin" 2>&1 7>&- 9>&-); then
        row "$idx" FAIL "$head" "$branch" "checkout failed: $(printf '%s' "$err" | tr '\t\n' '  ')"
        return
    fi
    now=$(git -C "$path" rev-parse HEAD 2>/dev/null 7>&- 9>&-) || now=""
    case "$now" in
        "$pin"*) ;;
        *)
            row "$idx" FAIL "$head" "$branch" "HEAD is ${now:-unreadable} after checkout"
            return
            ;;
    esac
    row "$idx" MOVED "$head" "$branch" -
    local f
    for f in "$workspace"/*/avocado-bakar.yml; do
        if [ -e "$f" ] && grep -q -F -- "$head" "$f" 7>&- 9>&-; then
            printf 'BAKAR_SYNC_NOTE\t%s\t%s\n' "$idx" "$(basename -- "$(dirname -- "$f")")"
        fi
    done
}
"""


def sync_script(workspace: Path, entries: list[PinnedRepo], rejected: dict[int, str]) -> str:
    """Render the bash script that syncs every embedded entry to its pin."""
    calls: list[str] = []
    for index, entry in enumerate(entries):
        if entry.pin is None or index in rejected:
            continue
        args = [str(index), entry.name, str(entry.path), entry.url, entry.pin]
        calls.append("sync_one " + " ".join(shlex.quote(a) for a in args))
    sent = len(calls)
    parts = [
        "set -u\n",
        lock_preamble(workspace),
        _SYNC_FUNCTION,
        "\n".join(calls) + "\n" if calls else "",
        f"printf 'BAKAR_SYNC_DONE\\t%s\\t%s\\n' \"$examined\" {sent}\n",
    ]
    return "".join(parts)


@dataclass(frozen=True)
class SyncRow:
    index: int
    status: str
    old: str | None
    branch: str | None
    detail: str


@dataclass(frozen=True)
class SyncReport:
    refused: str | None
    rows: list[SyncRow] = field(default_factory=list)
    notes: list[tuple[int, str]] = field(default_factory=list)
    revert_path: str | None = None
    examined: int = 0
    total: int = 0
    complete: bool = False


def run_local(script: str) -> tuple[int, str, str]:
    """Run ``script`` through local bash and return (status, stdout, stderr)."""
    try:
        proc = subprocess.run(
            ["bash", "-s"],
            input=script,
            capture_output=True,
            text=True,
            check=False,
            timeout=SYNC_TIMEOUT_SECONDS,
            start_new_session=True,
        )
    except subprocess.TimeoutExpired:
        return 255, "", f"sync timed out after {SYNC_TIMEOUT_SECONDS:.0f}s"
    return proc.returncode, proc.stdout, proc.stderr


def run_host(host: str, script: str) -> tuple[int, str, str]:
    """Run ``script`` on ``host`` and return (status, stdout, stderr)."""
    proc = remote_dispatch._ssh_bash_capture(host, script, timeout=SYNC_TIMEOUT_SECONDS)
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def _dash(value: str) -> str | None:
    return None if value == "-" else value


def parse_sync(stdout: str, entries: list[PinnedRepo], rejected: dict[int, str], rc: int) -> SyncReport:
    """Turn the script's output into a report, adding FAIL rows for rejected entries."""
    refused: str | None = None
    rows: list[SyncRow] = []
    notes: list[tuple[int, str]] = []
    revert_path: str | None = None
    done: tuple[int, int] | None = None
    for line in stdout.splitlines():
        fields = line.split("\t")
        tag = fields[0]
        if tag == "BAKAR_SYNC_REFUSED":
            refused = fields[1] if len(fields) > 1 else "refused"
        elif tag == "BAKAR_SYNC" and len(fields) >= 6:
            try:
                index = int(fields[1])
            except ValueError:
                continue
            rows.append(SyncRow(index, fields[2], _dash(fields[3]), _dash(fields[4]), "\t".join(fields[5:])))
        elif tag == "BAKAR_SYNC_NOTE" and len(fields) >= 3:
            try:
                notes.append((int(fields[1]), fields[2]))
            except ValueError:
                continue
        elif tag == "BAKAR_SYNC_REVERT" and len(fields) >= 2:
            revert_path = fields[1]
        elif tag == "BAKAR_SYNC_DONE" and len(fields) >= 3:
            try:
                done = (int(fields[1]), int(fields[2]))
            except ValueError:
                continue
    if refused is None and rc == EXIT_REFUSED:
        refused = "refused (exit 3)"
    total = sum(1 for e in entries if e.pin is not None)
    sent = total - len(rejected)
    for index, reason in rejected.items():
        rows.append(SyncRow(index, "FAIL", None, None, reason))
    rows.sort(key=lambda r: r.index)
    script_examined = done[0] if done else 0
    complete = refused is None and done is not None and done[0] == sent and done[1] == sent
    return SyncReport(
        refused=refused,
        rows=rows,
        notes=notes,
        revert_path=revert_path,
        examined=script_examined + len(rejected),
        total=total,
        complete=complete,
    )


def render_sync(report: SyncReport, entries: list[PinnedRepo]) -> list[str]:
    """Render the report as the lines the operator reads."""
    lines: list[str] = []
    if report.refused is not None:
        lines.append(f"REFUSED {report.refused}")
    moved = failed = 0
    bitbake_moved = False
    old_by_index: dict[int, str] = {}
    for row in report.rows:
        entry = entries[row.index]
        name = entry.name
        if row.status == "MOVED":
            moved += 1
            bitbake_moved = bitbake_moved or name == "bitbake"
            old_by_index[row.index] = row.old or ""
            pin = entry.pin or ""
            lines.append(f"MOVED {name} {row.branch or '-'} {(row.old or '')[:10]} -> {pin[:10]}")
        elif row.status == "AT_PIN":
            lines.append(f"AT PIN {name}")
        elif row.status == "MISSING":
            lines.append(f"MISSING {name} (kas clones it at build time)")
        elif row.status == "DIRTY":
            lines.append(f"LEFT {name} has local changes, not moved")
        else:
            failed += 1
            lines.append(f"FAIL {name} {row.detail}")
    grouped: dict[int, list[str]] = {}
    for index, build_dir in report.notes:
        grouped.setdefault(index, []).append(build_dir)
    for index, dirs in grouped.items():
        old = old_by_index.get(index, "")
        lines.append(f"build dirs pinning {entries[index].name} at {old[:12]}: {', '.join(dirs)}")
    if report.revert_path:
        lines.append(f"revert record: {report.revert_path}")
    if bitbake_moved:
        lines.append(
            "bitbake moved: restart the servers that loaded the old one with "
            "`bakar hashserv stop` then `bakar hashserv start`, and "
            "`bakar prserv stop` then `bakar prserv start`"
        )
    lines.append(f"batch complete: {report.examined}/{report.total} repos, moved={moved} failed={failed}")
    return lines
