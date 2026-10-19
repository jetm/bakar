"""``bakar status``: report one lifecycle state for a run.

Outcomes come only from a recorded exit status (the detached unit's sentinel,
then the run's terminal event). Liveness separates ``running`` from
``unknown`` and never produces ``succeeded`` or ``failed``.
"""

from __future__ import annotations

import json
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any

import typer
from click.exceptions import Exit as ClickExit

from bakar import detach_records
from bakar.build_stop import is_build_running
from bakar.commands._app import app, console
from bakar.commands._helpers import WorkspaceOption, _find_run
from bakar.commands.monitor import _build_progress
from bakar.commands.triage import _read_structured_failures, _resolve_triage_dirs
from bakar.observability import iter_run_events, last_run_event

_IS_ACTIVE_TIMEOUT = 5

STATES = ("starting", "running", "succeeded", "failed", "unknown")


@dataclass(frozen=True)
class StatusRecord:
    run_id: str | None
    unit: str | None
    host: str | None
    state: str
    exit_code: int | None
    run_dir: str | None
    log: str | None
    tasks_done: int | None
    tasks_total: int | None
    tasks_running: int | None
    elapsed_seconds: float | None
    failure: dict[str, str | None] | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "unit": self.unit,
            "host": self.host,
            "state": self.state,
            "exit_code": self.exit_code,
            "run_dir": self.run_dir,
            "log": self.log,
            "tasks_done": self.tasks_done,
            "tasks_total": self.tasks_total,
            "tasks_running": self.tasks_running,
            "elapsed_seconds": self.elapsed_seconds,
            "failure": self.failure,
        }


def _unit_active(unit: str) -> bool:
    try:
        proc = subprocess.run(
            ["systemctl", "--user", "is-active", unit],
            capture_output=True,
            text=True,
            timeout=_IS_ACTIVE_TIMEOUT,
            check=False,
            stdin=subprocess.DEVNULL,
        )
    except OSError, subprocess.SubprocessError:
        return False
    return proc.returncode == 0 and proc.stdout.strip() == "active"


def _terminal_exit_code(run_dir: Path) -> int | None:
    rec = last_run_event(run_dir / "events.jsonl", lambda r: r.get("event") in ("run_end", "run_error"))
    if rec is None:
        return None
    code = rec.get("exit_code")
    # bool is an int subclass; a boolean is not a recorded exit status.
    if isinstance(code, int) and not isinstance(code, bool):
        return code
    return None


def _progress(run_dir: Path | None) -> tuple[int | None, int | None, int | None, float | None]:
    if run_dir is None or not run_dir.is_dir():
        return None, None, None, None
    try:
        prog = _build_progress(run_dir)
    except OSError, ValueError:
        return None, None, None, None
    has_log = (run_dir / "bitbake_eventlog.json").is_file()
    total = prog.get("tasks_total")
    done = prog.get("tasks_done")
    running = prog.get("tasks_running")
    if total is None and not has_log:
        done = None
    if not has_log:
        running = None
    return done, total, running, prog.get("elapsed_seconds")


def _failure(run_dir: Path | None) -> dict[str, str | None] | None:
    if run_dir is None:
        return None
    failures = _read_structured_failures(run_dir)
    if failures:
        first = failures[0]
        return {"recipe": first.get("recipe"), "task": first.get("task"), "step": None, "error": None}
    return _event_failure(run_dir)


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _event_failure(run_dir: Path) -> dict[str, str | None] | None:
    """The reason a run failed before any task ran, from structured events only.

    The first ``step_fail`` names the step and its recorded text (``error``,
    else ``reason``); with none, the terminal ``run_error`` supplies its
    ``error_type`` as the step and its ``error`` text.
    """
    run_error: dict[str, Any] | None = None
    for rec in iter_run_events(run_dir / "events.jsonl"):
        event = rec.get("event")
        if event == "step_fail" and (step := _text(rec.get("step"))):
            return {
                "recipe": None,
                "task": None,
                "step": step,
                "error": _text(rec.get("error")) or _text(rec.get("reason")),
            }
        if event == "run_error":
            run_error = rec
    if run_error is not None and (step := _text(run_error.get("error_type"))):
        return {"recipe": None, "task": None, "step": step, "error": _text(run_error.get("error"))}
    return None


def _find_foreground(handle: str | None, workspace: Path | None) -> Path | None:
    try:
        runs_dirs, _root, _label, _tw = _resolve_triage_dirs(None, workspace)
    except ClickExit:
        return None
    found = _find_run(runs_dirs, handle)
    return found[0] if found else None


def _unit_run(unit: str) -> tuple[str | None, Path | None]:
    """Latest registered (run_id, run_dir) of ``unit``; (None, None) before registration."""
    regs = detach_records.read_registrations(unit)
    for rec in reversed(regs):
        run_id = rec.get("run_id")
        if isinstance(run_id, str) and run_id:
            raw_dir = rec.get("run_dir")
            return run_id, Path(raw_dir) if isinstance(raw_dir, str) and raw_dir else None
    return None, None


def _unit_log(unit: str) -> str | None:
    try:
        return str(detach_records.unit_paths(unit).log)
    except detach_records.DetachUnavailable:
        return None


def _outcome(code: int) -> tuple[str, int]:
    return ("succeeded" if code == 0 else "failed"), code


def _frontend_pid(run_id: str) -> int | None:
    """The front-end pid a run id embeds: the integer after its last hyphen."""
    tail = run_id.rsplit("-", 1)[-1]
    return int(tail) if tail.isdecimal() and "-" in run_id else None


def _frontend_alive(run_dir: Path) -> bool:
    """True while the run's front-end process is alive and is a bakar process.

    The cmdline check guards against the pid having been reused by something
    unrelated after the front end died.
    """
    pid = _frontend_pid(run_dir.name)
    if pid is None or pid <= 0:
        return False
    try:
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return False
    return b"bakar" in cmdline


def _run_live(run_dir: Path | None) -> bool:
    """Liveness only selects running over unknown; it never yields an outcome."""
    if run_dir is None:
        return False
    build_running, _pid, cmdline_ok = is_build_running(run_dir)
    return (build_running and cmdline_ok) or _frontend_alive(run_dir)


def _unit_state(unit: str, run_id: str | None) -> tuple[str, int | None] | None:
    """running/starting while ``unit`` is active with no sentinel; None once it has ended."""
    if detach_records.read_sentinel(unit) is None and _unit_active(unit):
        return ("starting" if run_id is None else "running"), None
    return None


def _state_for(
    *,
    unit: str | None,
    run_id: str | None,
    run_dir: Path | None,
    by_unit: bool = False,
) -> tuple[str, int | None]:
    """Resolve the state of a unit handle (``by_unit``) or of one run.

    A unit can carry several runs (one per release). Its sentinel is the
    unit-level outcome, so a unit handle reads it and never a per-run event; an
    inactive unit with no sentinel is ``unknown``. A run handle reports
    ``running`` while the unit is active, then its own terminal event, then the
    sentinel only when the run recorded none.
    """
    if unit is None:
        if run_dir is not None and (code := _terminal_exit_code(run_dir)) is not None:
            return _outcome(code)
        return ("running" if _run_live(run_dir) else "unknown"), None
    live = _unit_state(unit, run_id)
    if live is not None:
        return live
    # The unit can write its sentinel and be collected between the first
    # read and the liveness probe; read the sentinel again before concluding.
    sentinel = detach_records.read_sentinel(unit)
    if by_unit:
        return _outcome(sentinel) if sentinel is not None else ("unknown", None)
    if run_dir is not None and (code := _terminal_exit_code(run_dir)) is not None:
        return _outcome(code)
    if sentinel is not None:
        return _outcome(sentinel)
    return ("running" if _run_live(run_dir) else "unknown"), None


def compute_status(handle: str | None, *, workspace: Path | None = None) -> StatusRecord | None:
    """Resolve ``handle`` and report its state; None when it resolves to nothing."""
    unit: str | None = None
    run_id: str | None = None
    run_dir: Path | None = None
    by_unit = False

    if handle is not None and detach_records.is_unit_name(handle):
        unit = handle
        by_unit = True
        run_id, run_dir = _unit_run(unit)
        # An unregistered unit is only real while systemd still knows it or a
        # sentinel exists; otherwise the handle resolves to nothing.
        if (
            run_id is None
            and detach_records.read_sentinel(unit) is None
            and not _unit_active(unit)
            and detach_records.read_sentinel(unit) is None
        ):
            try:
                detach_records.runtime_dir()
            except detach_records.DetachUnavailable as exc:
                print(f"bakar status: detached records unavailable: {exc}", file=sys.stderr)
            return None
    elif handle is not None and (found_unit := detach_records.find_unit_for_run(handle)) is not None:
        unit = found_unit
        run_id, run_dir = _unit_run(unit)
        for rec in detach_records.read_registrations(unit):
            if rec.get("run_id") == handle:
                run_id = handle
                raw_dir = rec.get("run_dir")
                run_dir = Path(raw_dir) if isinstance(raw_dir, str) and raw_dir else None
    else:
        run_dir = _find_foreground(handle, workspace)
        if run_dir is None:
            return None
        run_id = run_dir.name
        unit = detach_records.find_unit_for_run(run_id)

    state, exit_code = _state_for(unit=unit, run_id=run_id, run_dir=run_dir, by_unit=by_unit)
    done, total, running, elapsed = _progress(run_dir)
    log = _unit_log(unit) if unit else (str(run_dir / "kas.log") if run_dir else None)
    return StatusRecord(
        run_id=run_id,
        unit=unit,
        host=None,
        state=state,
        exit_code=exit_code,
        run_dir=str(run_dir) if run_dir else None,
        log=log,
        tasks_done=done,
        tasks_total=total,
        tasks_running=running,
        elapsed_seconds=elapsed,
        failure=_failure(run_dir) if state == "failed" else None,
    )


def _fmt(value: object) -> str:
    return "?" if value is None else str(value)


def tasks_fragment(rec: StatusRecord) -> str:
    return f"tasks={_fmt(rec.tasks_done)}/{_fmt(rec.tasks_total)} running={_fmt(rec.tasks_running)}"


def render_summary(rec: StatusRecord) -> str:
    parts = [f"{rec.run_id or rec.unit} {rec.state}"]
    if rec.exit_code is not None:
        parts.append(f"exit={rec.exit_code}")
    parts.append(tasks_fragment(rec))
    if rec.failure:
        if rec.failure.get("step") is not None:
            parts.append(f"failed={rec.failure['step']}")
        else:
            parts.append(f"failed={_fmt(rec.failure.get('recipe'))}:{_fmt(rec.failure.get('task'))}")
    return " ".join(parts)


SSH_TRANSPORT_FAILURE = 255
REMOTE_STATUS_TIMEOUT = 30.0
EXIT_REFUSED = 2

# Same probe as remote_dispatch.preflight_remote, run here so the ssh exit
# status stays visible: that helper folds an unreachable host and a missing
# bakar into one message.
_PROBE_SCRIPT = "command -v bakar >/dev/null 2>&1 || exit 127\nbakar --version 2>&1\n"


def _report_transport(host: str, proc: subprocess.CompletedProcess[str]) -> None:
    if proc.stderr:
        print(proc.stderr, end="", file=sys.stderr)
    print(f"ssh to {host} failed (ssh exit status {proc.returncode})", file=sys.stderr)


def _preflight(host: str, *, transport_exit: int) -> int | None:
    """None when ``host`` can be queried, else the exit status to return.

    An unreachable host (ssh exit 255) is a transport failure and returns
    ``transport_exit``; a bakar that is missing or differs from the local one is
    a refusal (exit 2) because the caller must install bakar there.
    """
    from bakar.steps import remote_dispatch as rd

    if host.startswith("-"):
        print(
            f"bakar: invalid host {host!r}: must not begin with '-' (it would parse as an ssh option)",
            file=sys.stderr,
        )
        return EXIT_REFUSED
    proc = rd._ssh_bash_capture(host, _PROBE_SCRIPT)
    if proc.returncode == SSH_TRANSPORT_FAILURE:
        _report_transport(host, proc)
        return transport_exit
    if proc.returncode != 0:
        reason = (
            "bakar is not installed on the remote non-login PATH"
            if proc.returncode == 127
            else f"the remote probe failed (ssh exit status {proc.returncode})"
        )
        print(f"remote preflight failed for {host}: {reason}; install a matching bakar there", file=sys.stderr)
        if proc.stderr:
            print(proc.stderr.strip(), file=sys.stderr)
        return EXIT_REFUSED
    version = proc.stdout.strip() or proc.stderr.strip() or None
    if not rd.check_versions(version, assume_yes=False):
        return EXIT_REFUSED
    return None


def _fill_host(stdout: str, host: str) -> str:
    """Set a null ``host`` in a one-object JSON status to ``host``; anything else is unchanged."""
    try:
        obj = json.loads(stdout)
    except ValueError:
        return stdout
    if not isinstance(obj, dict) or "host" not in obj or obj["host"] is not None:
        return stdout
    obj["host"] = host
    return json.dumps(obj) + "\n"


def run_on_host(
    host: str,
    argv: list[str],
    *,
    capture_timeout: float | None,
    transport_exit: int,
    subcommand: str = "status",
) -> int:
    """Run ``bakar <argv>`` on ``host`` and mirror its stdout and exit status.

    The command goes over stdin to ``bash -s`` (never as an ssh argument) with
    every word shlex-quoted. A failed transport (ssh exit 255, which includes a
    timed-out capture) returns ``transport_exit`` and names the ssh status.
    ``subcommand`` names the remote command: only the status-record commands
    (status, wait) have a ``host`` field to fill from ``--on``.
    """
    from bakar.steps import remote_dispatch as rd

    refused = _preflight(host, transport_exit=transport_exit)
    if refused is not None:
        return refused
    script = shlex.join(["bakar", *argv]) + "\n"
    proc = rd._ssh_bash_capture(host, script, timeout=capture_timeout)  # type: ignore[arg-type]
    if proc.returncode == SSH_TRANSPORT_FAILURE:
        _report_transport(host, proc)
        return transport_exit
    if proc.stdout:
        fill = subcommand in ("status", "wait") and "--json" in argv
        print(_fill_host(proc.stdout, host) if fill else proc.stdout, end="")
    if proc.stderr:
        print(proc.stderr, end="", file=sys.stderr)
    return proc.returncode


OnOption = Annotated[
    str | None,
    typer.Option("--on", help="Query the run on this host over ssh instead of locally."),
]


@app.command("status")
def status(
    handle: Annotated[
        str | None,
        typer.Argument(help="Run id or detached unit name. Default: the newest run."),
    ] = None,
    json_out: Annotated[
        bool,
        typer.Option("--json", help="Emit one JSON object on stdout."),
    ] = False,
    workspace: WorkspaceOption = None,
    on: OnOption = None,
) -> None:
    """Report a run's state: starting, running, succeeded, failed or unknown.

    succeeded and failed come only from a recorded exit status; a run whose
    process vanished without one is unknown. Exits 0 whenever a state is
    reported and 2 when the handle does not resolve.
    """
    if on is not None:
        if workspace is not None:
            print("bakar status: -w/--workspace cannot be combined with --on", file=sys.stderr)
            raise typer.Exit(code=2)
        argv = ["status"]
        if handle is not None:
            argv.append(handle)
        if json_out:
            argv.append("--json")
        raise typer.Exit(code=run_on_host(on, argv, capture_timeout=REMOTE_STATUS_TIMEOUT, transport_exit=2))
    rec = compute_status(handle, workspace=workspace)
    if rec is None:
        console.print(f"No run found for {handle or 'the current workspace'}", markup=False, highlight=False)
        raise typer.Exit(code=2)
    if json_out:
        print(json.dumps(rec.to_dict()))
        return
    console.print(render_summary(rec), markup=False, highlight=False)
