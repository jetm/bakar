"""Next-step hints for agent callers, written to stderr only.

This module is the only reader of ``CLAUDECODE``. Hints go through the stderr
console so stdout stays machine-readable (``--json`` payloads, piped output).
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from bakar.commands._app import console

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

_WAIT_TIMEOUT = 540


def agent_mode(environ: Mapping[str, str] = os.environ) -> bool:
    """True only when running under Claude Code (``CLAUDECODE`` is exactly ``"1"``)."""
    return environ.get("CLAUDECODE") == "1"


def foreground_nudge() -> list[str]:
    """One line steering a foreground build toward a detached run."""
    return [f"hint: run with --detach, then `bakar wait <run_id> --timeout {_WAIT_TIMEOUT}` to avoid tool timeouts"]


def after_finish(run_id: str, rc: int) -> list[str]:
    """Follow-up for a finished run: triage a failure, report a success."""
    if rc != 0:
        return [f"hint: next: `bakar triage {run_id} --json`"]
    return [f"hint: next: `bakar report {run_id} --json`"]


def after_detach(handle: str, run_id: str | None, on: str | None = None) -> list[str]:
    """Follow-ups for a detached run: wait, status and stop."""
    suffix = f" --on {on}" if on else ""
    lines = [
        f"hint: wait: `bakar wait {handle} --timeout {_WAIT_TIMEOUT}{suffix}`",
        f"hint: status: `bakar status {handle} --json{suffix}`",
    ]
    if on:
        # `bakar stop` ignores --run when --on is given.
        lines.append(f"hint: stop: `bakar stop --on {on}`")
    elif run_id:
        lines.append(f"hint: stop: `bakar stop --run {run_id}`")
    else:
        lines.append(f"hint: `bakar status {handle} --json` names the run id once it is registered")
    return lines


def emit(lines: Iterable[str]) -> None:
    """Write hint lines to stderr, verbatim (no markup, no wrapping)."""
    for line in lines:
        console.print(line, markup=False, highlight=False, soft_wrap=True)
