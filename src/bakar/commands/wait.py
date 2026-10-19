"""``bakar wait``: poll a run until it reaches a terminal state.

The state comes from ``status.compute_status``; this module only owns the
polling loop, the progress lines and the exit-status mapping.
"""

from __future__ import annotations

import json
import math
import sys
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Annotated

import typer

from bakar.commands._app import app
from bakar.commands._helpers import WorkspaceOption
from bakar.commands.status import OnOption, StatusRecord, compute_status, run_on_host, tasks_fragment

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

DEFAULT_INTERVAL = 30.0
MIN_INTERVAL = 1.0
REMOTE_WAIT_SLACK = 60.0

EXIT_SUCCEEDED = 0
EXIT_FAILED = 1
EXIT_UNRESOLVED = 2
EXIT_UNKNOWN = 3
EXIT_TIMEOUT = 124

_TERMINAL_EXIT = {"succeeded": EXIT_SUCCEEDED, "failed": EXIT_FAILED, "unknown": EXIT_UNKNOWN}


def progress_line(rec: StatusRecord, handle: str, now: datetime) -> str:
    label = rec.run_id or rec.unit or handle
    return f"{now.astimezone(UTC).strftime('%H:%M:%SZ')} {label} {rec.state} {tasks_fragment(rec)}"


def run_wait(
    handle: str | None,
    *,
    timeout: float | None,
    interval: float,
    workspace: Path | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    emit: Callable[[str], None] = lambda line: print(line, file=sys.stderr, flush=True),
    status_fn: Callable[..., StatusRecord | None] | None = None,
) -> tuple[int, StatusRecord | None]:
    """Poll until a terminal state or the timeout; return (exit code, last record).

    Without a handle the target is resolved once, on the first poll, and pinned
    (its unit when it has one, else its run id), so a newer run started during
    the wait never takes over.
    """
    interval = max(interval, MIN_INTERVAL)
    start = clock()
    target = handle
    while True:
        rec = (status_fn or compute_status)(target, workspace=workspace)
        if rec is None:
            emit(f"No run found for {handle or 'the current workspace'}")
            return EXIT_UNRESOLVED, None
        if target is None:
            target = rec.unit or rec.run_id
        emit(progress_line(rec, handle or "", now()))
        if rec.state in _TERMINAL_EXIT:
            return _TERMINAL_EXIT[rec.state], rec
        pause = interval
        if timeout is not None:
            remaining = timeout - (clock() - start)
            if remaining <= 0:
                return EXIT_TIMEOUT, rec
            pause = min(interval, remaining)
        sleep(pause)


@app.command("wait")
def wait(
    handle: Annotated[
        str | None,
        typer.Argument(help="Run id or detached unit name. Default: the newest run."),
    ] = None,
    timeout: Annotated[
        float | None,
        typer.Option("--timeout", min=0, help="Give up after this many seconds (exit 124). Default: wait unbounded."),
    ] = None,
    interval: Annotated[
        float,
        typer.Option("--interval", help="Seconds between polls; values below 1 are raised to 1."),
    ] = DEFAULT_INTERVAL,
    json_out: Annotated[
        bool,
        typer.Option("--json", help="Print the final status object as the only stdout."),
    ] = False,
    workspace: WorkspaceOption = None,
    on: OnOption = None,
) -> None:
    """Wait for a run to finish and exit with its outcome.

    Exit 0 succeeded, 1 failed, 3 unknown, 124 timeout while starting or
    running, 2 when the handle does not resolve. Progress goes to stderr.
    """
    for name, value in (("--interval", interval), ("--timeout", timeout)):
        if value is not None and not math.isfinite(value):
            print(f"bakar wait: {name} must be a finite number, got {value}", file=sys.stderr)
            raise typer.Exit(code=EXIT_UNRESOLVED)
    if on is not None:
        if workspace is not None:
            print("bakar wait: -w/--workspace cannot be combined with --on", file=sys.stderr)
            raise typer.Exit(code=EXIT_UNRESOLVED)
        argv = ["wait"]
        if handle is not None:
            argv.append(handle)
        if timeout is not None:
            argv += ["--timeout", str(timeout)]
        argv += ["--interval", str(interval)]
        if json_out:
            argv.append("--json")
        capture = None if timeout is None else timeout + REMOTE_WAIT_SLACK
        raise typer.Exit(code=run_on_host(on, argv, capture_timeout=capture, transport_exit=EXIT_UNKNOWN))
    code, rec = run_wait(handle, timeout=timeout, interval=interval, workspace=workspace)
    if json_out and rec is not None:
        print(json.dumps(rec.to_dict()))
    raise typer.Exit(code=code)
