"""Move the shared clones of a release workspace onto their pins.

The script this module renders runs on whichever machine owns the workspace.
It changes nothing but ``HEAD`` and the working tree of a clone that is clean
and off its pin: no branch, ref, config or file outside the clone is touched,
and nothing is forced, hard-reset, wiped or shelved. A dirty clone, a missing clone and
a side checkout are left alone. Both workspace locks are taken before any
clone moves, and a build that uses the workspace refuses the whole run.

Entries are identified by their index in the caller's list and never by name in
the script's output, so a hostile name cannot forge a line.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from bakar.steps import pin_probe

if TYPE_CHECKING:
    from pathlib import Path

    from bakar.steps.repo_reuse import PinnedRepo

EXIT_REFUSED = 3
SYNC_TIMEOUT_SECONDS = 3600.0

# kas accepts an abbreviated pin. Only hex is embedded; the script reports one git
# resolves to HEAD as AT_PIN and refuses to move a clone to anything short of 40.
_HEX_PIN_RE = re.compile(r"[0-9a-fA-F]{4,40}")
# flock's status for "another process holds it", distinct from 1 and from 66 (cannot open).
_FLOCK_HELD = 75


def lock_preamble(workspace: Path) -> str:
    """Bash taking the in-use and repos locks and probing every build directory.

    Both locks stay held on fds 7 and 9 until the script exits; every child
    started afterwards closes them so a git hook cannot outlive the script
    holding them. A lock file that merely exists refuses nothing: only a live
    ``flock`` holder does. A lock file that cannot be opened refuses with its
    own line, so it is never reported as a build holding the lock: every probe
    runs ``flock -E 75``, and only status 75 means another holder.
    """
    ws = shlex.quote(str(workspace))
    held = _FLOCK_HELD
    return f"""workspace={ws}
refuse() {{
    printf 'BAKAR_SYNC_REFUSED\\t%s\\n' "$1"
    exit {EXIT_REFUSED}
}}
mkdir -p -- "$workspace/.bakar" 2>/dev/null || refuse "cannot open $workspace/.bakar"
{{ exec 7>>"$workspace/.bakar/repos-in-use.lock"; }} 2>/dev/null ||
    refuse "cannot open $workspace/.bakar/repos-in-use.lock"
flock -n -E {held} -x 7 2>/dev/null
case $? in
    0) ;;
    {held}) refuse "a bakar build or shell session holds $workspace/.bakar/repos-in-use.lock" ;;
    *) refuse "cannot lock $workspace/.bakar/repos-in-use.lock" ;;
esac
{{ exec 9>>"$workspace/.bakar/repos.lock"; }} 2>/dev/null || refuse "cannot open $workspace/.bakar/repos.lock"
flock -n -E {held} -x 9 2>/dev/null
case $? in
    0) ;;
    {held}) refuse "another run holds $workspace/.bakar/repos.lock" ;;
    *) refuse "cannot lock $workspace/.bakar/repos.lock" ;;
esac
for build_lock in "$workspace"/*/build/bitbake.lock; do
    [ -e "$build_lock" ] || continue
    flock -n -E {held} -s "$build_lock" true 2>/dev/null 7>&- 9>&-
    case $? in
        0) ;;
        {held}) refuse "a build holds $build_lock (build directory ${{build_lock%/build/bitbake.lock}})" ;;
        *) refuse "cannot open $build_lock" ;;
    esac
done
"""


def validate_sync_entries(entries: list[PinnedRepo]) -> dict[int, str]:
    """Return index -> reason for each pinned entry that must not be embedded."""
    rejected: dict[int, str] = {}
    for index, entry in enumerate(entries):
        if entry.pin is None:
            continue
        if not _HEX_PIN_RE.fullmatch(entry.pin):
            rejected[index] = "pin is not a full 40-character commit id"
        elif entry.url.startswith("-"):
            rejected[index] = "url begins with '-'"
        elif any(c in value for value in (entry.name, str(entry.path)) for c in "\t\n"):
            rejected[index] = "name or path contains a tab or newline"
    return rejected


_SYNC_FUNCTION = r"""
examined=0
revert=""
real_ws=$(realpath -m -- "$workspace")

row() {
    printf 'BAKAR_SYNC\t%s\t%s\t%s\t%s\t%s\n' "$1" "$2" "$3" "$4" "$5"
}

sync_one() {
    local idx=$1 name=$2 path=$3 url=$4 pin=$5
    local real top head branch st err now rc
    examined=$((examined + 1))
    # git -C walks up from a plain directory into an enclosing clone, so a path
    # outside the workspace or below another clone would move the wrong repository.
    real=$(realpath -m -- "$path" 2>/dev/null) || real=""
    case "$real" in
        "$real_ws"/*) ;;
        *)
            row "$idx" FAIL - - "path is not under the workspace"
            return
            ;;
    esac
    if [ ! -d "$real" ]; then
        row "$idx" MISSING - - -
        return
    fi
    top=$(git -C "$real" rev-parse --show-toplevel 2>/dev/null 7>&- 9>&- </dev/null) || top=""
    if [ "$top" != "$real" ]; then
        row "$idx" FAIL - - "path is not the top of a clone"
        return
    fi
    head=$(git -C "$real" rev-parse HEAD 2>/dev/null 7>&- 9>&- </dev/null) || {
        row "$idx" FAIL - - "cannot read HEAD"
        return
    }
    # The full ref, not --short: --short prints heads/<b> when a tag <b> exists.
    branch=$(git -C "$real" symbolic-ref -q HEAD 2>/dev/null 7>&- 9>&- </dev/null) || branch=-
    branch=${branch#refs/heads/}
    [ -n "$branch" ] || branch=-
    st=$(git -C "$real" --no-optional-locks status --porcelain 2>/dev/null 7>&- 9>&- </dev/null) || {
        row "$idx" FAIL "$head" "$branch" "cannot read status"
        return
    }
    # Two commits can share an abbreviated pin's prefix, so a short pin counts as
    # reached only when git resolves it to exactly HEAD; an ambiguous one does not resolve.
    local at=-
    if [ "${#pin}" -eq 40 ]; then
        [ "$head" = "$pin" ] && at=yes
    elif [ "$(git -C "$real" rev-parse --verify -q "$pin^{commit}" 2>/dev/null 7>&- 9>&- </dev/null)" = "$head" ]; then
        at=yes
    fi
    case "$at" in
        yes)
            if [ -n "$st" ]; then
                row "$idx" AT_PIN "$head" "$branch" "has local changes"
            else
                row "$idx" AT_PIN "$head" "$branch" -
            fi
            return
            ;;
    esac
    # An abbreviated pin is only ever compared, never checked out.
    if [ "${#pin}" -ne 40 ]; then
        row "$idx" FAIL "$head" "$branch" "pin is not a full 40-character commit id"
        return
    fi
    if [ -n "$st" ]; then
        row "$idx" DIRTY "$head" "$branch" -
        return
    fi
    if ! git -C "$real" cat-file -e "$pin^{commit}" 2>/dev/null 7>&- 9>&- </dev/null; then
        if ! err=$(git -C "$real" fetch --no-tags -q --end-of-options "$url" "$pin" 2>&1 7>&- 9>&- </dev/null); then
            row "$idx" FAIL "$head" "$branch" "fetch failed: $(printf '%s' "$err" | tr '\t\n' '  ')"
            return
        fi
    fi
    if [ -z "$revert" ]; then
        local stamp
        stamp=$(date +%Y%m%dT%H%M%S)
        # mktemp creates a new file under an unpredictable name with O_EXCL, so a
        # symlink or file planted in the directory is never followed or appended to.
        mkdir -p -- "$workspace/.bakar/sync-revert" 2>/dev/null &&
            revert=$(mktemp -- "$workspace/.bakar/sync-revert/$stamp-$$-XXXXXX.tsv" 2>/dev/null 7>&- 9>&-) || {
            revert=""
            row "$idx" FAIL "$head" "$branch" "cannot create the revert record"
            return
        }
        printf 'BAKAR_SYNC_REVERT\t%s\n' "$revert"
    fi
    printf '%s\t%s\t%s\t%s\t%s\n' "$name" "$path" "$head" "$branch" "$pin" >> "$revert" || {
        row "$idx" FAIL "$head" "$branch" "cannot write the revert record"
        return
    }
    if ! err=$(git -C "$real" checkout -q --no-overwrite-ignore --detach "$pin" 2>&1 7>&- 9>&- </dev/null); then
        row "$idx" FAIL "$head" "$branch" "checkout failed: $(printf '%s' "$err" | tr '\t\n' '  ')"
        return
    fi
    now=$(git -C "$real" rev-parse HEAD 2>/dev/null 7>&- 9>&- </dev/null) || now=""
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
        # rev-parse prints lowercase hex, and the script compares by prefix.
        args = [str(index), entry.name, str(entry.path), entry.url, entry.pin.lower()]
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
    timed_out: str | None = None


# Appended by _run, never printed by the script, so it carries no BAKAR_ prefix.
_TIMEOUT_TAG = "PIN_SYNC_TIMED_OUT"


def _run(script: str, host: str | None) -> tuple[int, str, str]:
    rc, out, err, timed_out = pin_probe.run_bash(script, host=host, timeout=SYNC_TIMEOUT_SECONDS)
    if timed_out:
        # Keep the rows the script printed before the bound elapsed: clones it moved and
        # its revert record are real. An unterminated last line is a cut-off row and is dropped.
        out = out[: out.rfind("\n") + 1]
        if host is None:
            note = f"did not finish after {SYNC_TIMEOUT_SECONDS:.0f}s and may still be running"
        else:
            note = f"did not finish after {SYNC_TIMEOUT_SECONDS:.0f}s on {host} and may still be running there"
        out += f"{_TIMEOUT_TAG}\t{note}; check before re-running\n"
    return rc, out, err


def run_local(script: str) -> tuple[int, str, str]:
    """Run ``script`` through local bash and return (status, stdout, stderr)."""
    return _run(script, None)


def run_host(host: str, script: str) -> tuple[int, str, str]:
    """Run ``script`` on ``host`` and return (status, stdout, stderr)."""
    return _run(script, host)


def parse_sync(stdout: str, entries: list[PinnedRepo], rejected: dict[int, str], rc: int) -> SyncReport:
    """Turn the script's output into a report, adding FAIL rows for rejected entries."""
    refused: str | None = None
    rows: list[SyncRow] = []
    notes: list[tuple[int, str]] = []
    revert_path: str | None = None
    done: tuple[int, int] | None = None
    timed_out: str | None = None
    # Only entries the script was sent can come back; a row or note naming anything else is
    # host noise, and indexing the caller's list with it would raise or pick the wrong repo.
    sent_indexes = {i for i, e in enumerate(entries) if e.pin is not None and i not in rejected}
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
            if index not in sent_indexes:
                continue
            rows.append(
                SyncRow(index, fields[2], pin_probe._text(fields[3]), pin_probe._text(fields[4]), "\t".join(fields[5:]))
            )
        elif tag == "BAKAR_SYNC_NOTE" and len(fields) >= 3:
            try:
                index = int(fields[1])
            except ValueError:
                continue
            if index in sent_indexes:
                notes.append((index, fields[2]))
        elif tag == "BAKAR_SYNC_REVERT" and len(fields) >= 2:
            revert_path = fields[1]
        elif tag == _TIMEOUT_TAG and len(fields) >= 2:
            timed_out = fields[1]
        elif tag == "BAKAR_SYNC_DONE" and len(fields) >= 3:
            try:
                done = (int(fields[1]), int(fields[2]))
            except ValueError:
                continue
    if refused is None and rc == EXIT_REFUSED:
        refused = "refused (exit 3)"
    total = sum(1 for e in entries if e.pin is not None)
    sent = total - len(rejected)
    # Without a done line (a timeout cut the script short) the rows it printed are what it examined.
    script_examined = done[0] if done else len(rows)
    for index, reason in rejected.items():
        rows.append(SyncRow(index, "FAIL", None, None, reason))
    rows.sort(key=lambda r: r.index)
    complete = refused is None and timed_out is None and done is not None and done[0] == sent and done[1] == sent
    return SyncReport(
        refused=refused,
        rows=rows,
        notes=notes,
        revert_path=revert_path,
        examined=script_examined + len(rejected),
        total=total,
        complete=complete,
        timed_out=timed_out,
    )


def _restart_advice(workspace: Path, host: str | None) -> str:
    target = f"-w {shlex.quote(str(workspace))}"
    where = f", on {host}," if host is not None else ""
    return (
        f"bitbake moved: restart the servers that loaded the old one{where} with "
        f"`bakar hashserv stop {target}` then `bakar hashserv start {target}`, and "
        f"`bakar prserv stop {target}` then `bakar prserv start {target}`"
    )


def render_sync(
    report: SyncReport, entries: list[PinnedRepo], *, workspace: Path, host: str | None = None
) -> list[str]:
    """Render the report as the lines the operator reads.

    ``workspace`` and ``host`` are where the sync ran, so the restart advice
    names the daemons it means rather than whichever workspace the reader is in.
    """
    lines: list[str] = []
    if report.refused is not None:
        lines.append(f"REFUSED {report.refused}")
    moved = failed = 0
    bitbake_moved = False
    dirs_by_index: dict[int, list[str]] = {}
    for index, build_dir in report.notes:
        dirs_by_index.setdefault(index, []).append(build_dir)
    for row in report.rows:
        entry = entries[row.index]
        name = entry.name
        if row.status == "MOVED":
            moved += 1
            bitbake_moved = bitbake_moved or name == "bitbake"
            old = row.old or ""
            pin = entry.pin or ""
            lines.append(f"MOVED {name} {row.branch or '-'} {old[:10]} -> {pin[:10]}")
            if row.index in dirs_by_index:
                lines.append(f"build dirs pinning {name} at {old[:12]}: {', '.join(dirs_by_index[row.index])}")
        elif row.status == "AT_PIN":
            dirty = " (has local changes)" if row.detail == "has local changes" else ""
            lines.append(f"AT PIN {name}{dirty}")
        elif row.status == "MISSING":
            lines.append(f"MISSING {name} (kas clones it at build time)")
        elif row.status == "DIRTY":
            lines.append(f"LEFT {name} has local changes, not moved")
        else:
            failed += 1
            lines.append(f"FAIL {name} {row.detail}")
    if report.revert_path:
        lines.append(f"revert record: {report.revert_path}")
    if report.timed_out:
        lines.append(f"sync {report.timed_out}")
    if bitbake_moved:
        lines.append(_restart_advice(workspace, host))
    lines.append(f"batch complete: {report.examined}/{report.total} repos, moved={moved} failed={failed}")
    return lines


_REVERT_FUNCTION = r"""
examined=0
total=$(grep -c '' "$record" 2>/dev/null) || total=0
real_ws=$(realpath -m -- "$workspace")

rrow() {
    printf 'BAKAR_REVERT\t%s\t%s\t%s\n' "$1" "$2" "$3"
}

revert_one() {
    local line=$1 path=$2 head=$3 branch=$4 pin=$5
    local real top st err now at tip
    examined=$((examined + 1))
    if ! [[ $head =~ ^[0-9a-fA-F]{40}$ ]]; then
        rrow "$line" INVALID "head is not 40 hexadecimal characters"
        return
    fi
    # rev-parse prints lowercase hex, and every comparison below is exact.
    head=${head,,}
    if [ -n "$pin" ] && ! [[ $pin =~ ^[0-9a-fA-F]{40}$ ]]; then
        rrow "$line" INVALID "pin is not 40 hexadecimal characters"
        return
    fi
    if [ -z "$path" ]; then
        rrow "$line" INVALID "path is empty"
        return
    fi
    real=$(realpath -m -- "$path" 2>/dev/null) || real=""
    case "$real" in
        "$real_ws"/*) ;;
        *)
            rrow "$line" OUTSIDE "path is not under the workspace"
            return
            ;;
    esac
    if [ "$branch" != - ]; then
        case "$branch" in
            -*)
                rrow "$line" INVALID "branch is not a valid branch name"
                return
                ;;
        esac
        at=$(git check-ref-format --branch "$branch" 2>/dev/null </dev/null 7>&- 8>&- 9>&-) || at=""
        if [ "$at" != "$branch" ]; then
            rrow "$line" INVALID "branch is not a valid branch name"
            return
        fi
    fi
    if [ ! -d "$real" ]; then
        rrow "$line" FAIL "clone is missing"
        return
    fi
    top=$(git -C "$real" rev-parse --show-toplevel 2>/dev/null </dev/null 7>&- 8>&- 9>&-) || top=""
    if [ "$top" != "$real" ]; then
        rrow "$line" FAIL "path is not the top of a clone"
        return
    fi
    st=$(git -C "$real" --no-optional-locks status --porcelain 2>/dev/null </dev/null 7>&- 8>&- 9>&-) || {
        rrow "$line" FAIL "cannot read status"
        return
    }
    if [ -n "$st" ]; then
        rrow "$line" DIRTY "local changes, not restored"
        return
    fi
    if [ -n "$pin" ]; then
        # Older records carry no pin. With one, a clone that has moved on since the sync
        # is left alone, because the checkout below would orphan whatever was committed there.
        # A clone still at the recorded head is one whose sync checkout failed: restoring it is safe.
        now=$(git -C "$real" rev-parse HEAD 2>/dev/null </dev/null 7>&- 8>&- 9>&-) || {
            rrow "$line" FAIL "cannot read HEAD"
            return
        }
        if [ "$now" != "${pin,,}" ] && [ "$now" != "$head" ]; then
            rrow "$line" MOVED_ON "clone is no longer at the commit the sync set, not restored"
            return
        fi
    fi
    if ! git -C "$real" cat-file -e "$head^{commit}" 2>/dev/null </dev/null 7>&- 8>&- 9>&-; then
        rrow "$line" FAIL "recorded commit is not in the clone"
        return
    fi
    if [ "$branch" != - ]; then
        tip=$(git -C "$real" rev-parse -q --verify "refs/heads/$branch" 2>/dev/null </dev/null 7>&- 8>&- 9>&-) || tip=""
        if [ "$tip" = "$head" ]; then
            if ! err=$(git -C "$real" checkout -q --no-overwrite-ignore "$branch" -- \
                2>&1 </dev/null 7>&- 8>&- 9>&-); then
                rrow "$line" FAIL "checkout failed: $(printf '%s' "$err" | tr '\t\n' '  ')"
                return
            fi
            now=$(git -C "$real" rev-parse HEAD 2>/dev/null </dev/null 7>&- 8>&- 9>&-) || now=""
            at=$(git -C "$real" symbolic-ref -q HEAD 2>/dev/null </dev/null 7>&- 8>&- 9>&-) || at=""
            if [ "$now" = "$head" ] && [ "$at" = "refs/heads/$branch" ]; then
                rrow "$line" RESTORED "on the recorded branch"
            else
                rrow "$line" FAIL "HEAD is ${now:-unreadable} after checkout"
            fi
            return
        fi
    fi
    if ! err=$(git -C "$real" checkout -q --no-overwrite-ignore --detach "$head" 2>&1 </dev/null 7>&- 8>&- 9>&-); then
        rrow "$line" FAIL "checkout failed: $(printf '%s' "$err" | tr '\t\n' '  ')"
        return
    fi
    now=$(git -C "$real" rev-parse HEAD 2>/dev/null </dev/null 7>&- 8>&- 9>&-) || now=""
    if [ "$now" != "$head" ]; then
        rrow "$line" FAIL "HEAD is ${now:-unreadable} after checkout"
        return
    fi
    if [ "$branch" = - ]; then
        rrow "$line" DETACHED "recorded HEAD was detached"
    else
        rrow "$line" DETACHED "recorded branch moved or is gone"
    fi
}

if ! { exec 8<"$record"; } 2>/dev/null; then
    printf 'BAKAR_SYNC_REFUSED\t%s\n' "revert record $record is not readable"
    exit 3
fi
lineno=0
name="" path="" head="" branch="" pin="" rest=""
while IFS=$'\t' read -r name path head branch pin rest <&8 || [ -n "$name$path$head" ]; do
    lineno=$((lineno + 1))
    if [ -z "$branch" ] || [ -n "$rest" ]; then
        examined=$((examined + 1))
        rrow "$lineno" INVALID "line does not have four or five fields"
    else
        revert_one "$lineno" "$path" "$head" "$branch" "$pin"
    fi
    name="" path="" head="" branch="" pin="" rest=""
done
exec 8<&-
"""


def revert_script(workspace: Path, record: str) -> str:
    """Render the bash script that restores the clones named in ``record``.

    ``record`` is read on the machine the script runs on. A clone is put back
    on its recorded branch only when that branch still points at the recorded
    HEAD; otherwise it is checked out detached at that HEAD.
    """
    return "".join(
        [
            "set -u\n",
            f"record={shlex.quote(record)}\n",
            # Checked before the lock preamble, which creates <workspace>/.bakar and two lock files.
            'if [ ! -f "$record" ]; then\n'
            "    printf 'BAKAR_SYNC_REFUSED\\t%s\\n' \"revert record $record not found\"\n"
            f"    exit {EXIT_REFUSED}\n"
            "fi\n",
            'if [ ! -r "$record" ]; then\n'
            "    printf 'BAKAR_SYNC_REFUSED\\t%s\\n' \"revert record $record is not readable\"\n"
            f"    exit {EXIT_REFUSED}\n"
            "fi\n",
            lock_preamble(workspace),
            _REVERT_FUNCTION,
            'printf \'BAKAR_REVERT_DONE\\t%s\\t%s\\n\' "$examined" "$total"\n',
        ]
    )


@dataclass(frozen=True)
class RevertReport:
    refused: str | None
    rows: list[tuple[int, str, str]]
    examined: int
    total: int
    complete: bool


def parse_revert(stdout: str, rc: int) -> RevertReport:
    """Turn the revert script's output into a report."""
    refused: str | None = None
    rows: list[tuple[int, str, str]] = []
    done: tuple[int, int] | None = None
    for line in stdout.splitlines():
        fields = line.split("\t")
        tag = fields[0]
        if tag == "BAKAR_SYNC_REFUSED":
            refused = fields[1] if len(fields) > 1 else "refused"
        elif tag == "BAKAR_REVERT" and len(fields) >= 4:
            try:
                number = int(fields[1])
            except ValueError:
                continue
            rows.append((number, fields[2], "\t".join(fields[3:])))
        elif tag == "BAKAR_REVERT_DONE" and len(fields) >= 3:
            try:
                done = (int(fields[1]), int(fields[2]))
            except ValueError:
                continue
    if refused is None and rc == EXIT_REFUSED:
        refused = "refused (exit 3)"
    examined, total = done if done else (len(rows), 0)
    complete = refused is None and done is not None and done[0] == done[1]
    return RevertReport(refused=refused, rows=rows, examined=examined, total=total, complete=complete)


def render_revert(report: RevertReport) -> list[str]:
    """Render the revert report as the lines the operator reads."""
    lines: list[str] = []
    if report.refused is not None:
        lines.append(f"REFUSED {report.refused}")
    restored = failed = 0
    for number, status, detail in report.rows:
        if status in ("RESTORED", "DETACHED"):
            restored += 1
        else:
            failed += 1
        lines.append(f"{status} line {number} {detail}")
    lines.append(f"batch complete: {report.examined}/{report.total} repos, restored={restored} failed={failed}")
    return lines
