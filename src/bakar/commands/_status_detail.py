"""Assembly for ``bakar status --detail``.

Pure functions that turn the already-computed task progress, the run directory
and the host measurement into the six ``detail`` keys, and render them as
human lines. Nothing here decides a state, exit code or failure: ``last_line``
is informational text only.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from bakar import host_snapshot, observability

_MAX_RUNNING = 16
_SHOWN_RUNNING = 5
_TAIL_BYTES = 64 * 1024
_LAST_LINE_CHARS = 200

_ANSI_CSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_ANSI_OSC = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?")
# C0, DEL, C1, plus bidi marks/embeddings/isolates and the line/paragraph separators.
_INVISIBLE = (0x061C, 0x200E, 0x200F, 0x2028, 0x2029, *range(0x202A, 0x202F), *range(0x2066, 0x206A))
_CONTROL = re.compile("[\x00-\x1f\x7f-\x9f" + "".join(map(chr, _INVISIBLE)) + "]")


def clean_text(text: str) -> str:
    """Strip escape sequences, control, bidi and line-separator characters."""
    text = _ANSI_OSC.sub("", _ANSI_CSI.sub("", text))
    return _CONTROL.sub("", text.replace("\t", " "))


def _clean_line(line: str) -> str:
    line = line.rstrip("\r").rsplit("\r", 1)[-1]
    return clean_text(line).strip()[:_LAST_LINE_CHARS]


def last_line(path: Path) -> str | None:
    """Return the last visible line of ``path``, reading only its final 64 KiB."""
    try:
        with path.open("rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - _TAIL_BYTES))
            data = fh.read(_TAIL_BYTES)
    except OSError:
        return None
    text = data.decode("utf-8", errors="replace")
    for raw in reversed(text.split("\n")):
        line = _clean_line(raw)
        if line:
            return line
    return None


def _longest_running(progress: dict, now: float) -> list[dict]:
    rows: list[dict] = []
    for entry in progress.get("running") or []:
        started = entry.get("started")
        if isinstance(started, bool) or not isinstance(started, (int, float)):
            seconds = None
        else:
            seconds = max(0, int(now - started))
        rows.append(
            {
                "recipe": _clean_name(entry.get("recipe")),
                "task": _clean_name(entry.get("task")),
                "seconds": seconds,
            }
        )
    rows.sort(key=lambda r: (r["seconds"] is None, -(r["seconds"] or 0)))
    return rows[:_MAX_RUNNING]


def build_detail(
    *,
    state: str,
    run_dir: Path | None,
    progress: dict | None,
    has_eventlog: bool,
    unit_log: Path | None,
    now: float,
    measure: Callable[..., dict] = host_snapshot.measure,
) -> dict:
    """Return the six ``detail`` keys for one run."""
    running = None
    if state == "running" and has_eventlog and progress is not None:
        running = _longest_running(progress, now)

    failed = None
    if has_eventlog and progress is not None:
        failed = {
            "count": progress.get("tasks_failed"),
            "names": [
                f"{_clean_name(f.get('recipe')) or '?'}:{_clean_name(f.get('task')) or '?'}"
                for f in progress.get("failures") or []
            ],
        }

    line = None
    if run_dir is not None and (run_dir / "kas.log").exists():
        line = last_line(run_dir / "kas.log")
    elif unit_log is not None:
        line = last_line(unit_log)

    if run_dir is None:
        host = measure(None, None)
    else:
        tmpdir, sstate = observability.last_build_paths(run_dir)
        build_dir = run_dir.parent.parent
        host = measure(tmpdir if tmpdir is not None else build_dir, sstate)
        # The recorded TMPDIR may not exist yet early in a first build; the run's
        # own build directory is then the next best answer for "build" free space.
        if tmpdir is not None and (host.get("disk_free_gb") or {}).get("build") is None and build_dir.exists():
            fallback = measure(build_dir, None).get("disk_free_gb") or {}
            host = {**host, "disk_free_gb": {**(host.get("disk_free_gb") or {}), "build": fallback.get("build")}}

    return {
        "running_tasks": running,
        "failed_tasks": failed,
        "last_line": line,
        "disk_free_gb": host.get("disk_free_gb"),
        "mem_available_mb": host.get("mem_available_mb"),
        "pressure": host.get("pressure"),
    }


def _q(value: Any) -> str:
    return "?" if value is None else str(value)


def _clean_name(value: Any) -> str | None:
    return None if value is None else clean_text(str(value))


def _age(seconds: int | None) -> str:
    if seconds is None:
        return "?"
    return f"{seconds // 60}m{seconds % 60:02d}s"


def detail_lines(summary: str, detail: dict, *, running_total: int | None = None) -> list[str]:
    """Render the summary line plus the detail lines for human output.

    ``running_total`` is the real number of running tasks; ``detail`` carries
    at most 16 of them, so the ``(+N more)`` count comes from the total.
    """
    lines = [summary]

    running = detail.get("running_tasks")
    if running:
        total = max(running_total or 0, len(running))
        shown = ", ".join(
            f"{_q(_clean_name(r.get('recipe')))}:{_q(_clean_name(r.get('task')))} {_age(r.get('seconds'))}"
            for r in running[:_SHOWN_RUNNING]
        )
        if total > _SHOWN_RUNNING:
            shown += f" (+{total - _SHOWN_RUNNING} more)"
        lines.append(f"running: {shown}")

    failed = detail.get("failed_tasks")
    if failed and (failed.get("count") or 0) > 0:
        names = [clean_text(str(n)) for n in failed.get("names") or []]
        count = failed["count"]
        listed = ", ".join(names)
        if isinstance(count, int) and count > len(names):
            listed += f"{', ' if names else ''}+{count - len(names)} more"
        lines.append(f"failed: {count} ({listed})")

    disk = detail.get("disk_free_gb") or {}
    pressure = detail.get("pressure") or {}
    lines.append(
        f"host: disk build {_q(disk.get('build'))}G sstate {_q(disk.get('sstate'))}G free, "
        f"mem {_q(detail.get('mem_available_mb'))}M available, "
        f"pressure cpu {_q(pressure.get('cpu'))} io {_q(pressure.get('io'))} mem {_q(pressure.get('memory'))}"
    )
    lines.append(f"last: {_q(detail.get('last_line'))}")
    return lines
