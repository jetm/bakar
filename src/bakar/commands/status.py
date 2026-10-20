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
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Annotated, Any

import typer
from click.exceptions import Exit as ClickExit

from bakar import detach_records, observability
from bakar.build_stop import is_build_running
from bakar.commands._app import app, console
from bakar.commands._helpers import WorkspaceOption, _find_run, _find_workspace_from_cwd
from bakar.commands._status_detail import build_detail, detail_lines
from bakar.commands.monitor import _build_progress
from bakar.commands.triage import _read_structured_failures, _resolve_triage_dirs
from bakar.observability import frontend_alive as _frontend_alive
from bakar.observability import frontend_pid as _frontend_pid  # noqa: F401 - tests reach it as status._frontend_pid
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
        return asdict(self)


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


def _read_progress(run_dir: Path | None) -> tuple[dict[str, Any] | None, bool]:
    """The one ``_build_progress`` result for ``run_dir`` and whether its event log exists."""
    if run_dir is None or not run_dir.is_dir():
        return None, False
    try:
        prog = _build_progress(run_dir)
    except OSError, ValueError:
        return None, False
    return prog, (run_dir / "bitbake_eventlog.json").is_file()


def _progress_counts(
    prog: dict[str, Any] | None, has_log: bool
) -> tuple[int | None, int | None, int | None, float | None]:
    if prog is None:
        return None, None, None, None
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
    if observability.read_stop_request(run_dir) is not None:
        return {"recipe": None, "task": None, "step": observability.STOPPED_STEP, "error": observability.STOPPED_ERROR}
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
                "error": observability.step_fail_text(rec),
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


@dataclass(frozen=True)
class _Resolved:
    """One resolution of a handle: the record plus what ``--detail`` reuses.

    ``progress`` is the single ``_build_progress`` result, so the detail path
    never reads the event log a second time.
    """

    record: StatusRecord
    progress: dict[str, Any] | None
    has_eventlog: bool
    run_dir: Path | None
    unit_log: Path | None


def compute_status(handle: str | None, *, workspace: Path | None = None) -> StatusRecord | None:
    """Resolve ``handle`` and report its state; None when it resolves to nothing."""
    resolved = _resolve(handle, workspace)
    return resolved.record if resolved is not None else None


def _resolve(handle: str | None, workspace: Path | None) -> _Resolved | None:
    unit: str | None = None
    run_id: str | None = None
    run_dir: Path | None = None
    by_unit = False

    # Outside any workspace, which is where `--on HOST` runs, the newest
    # detached unit stands in for the missing handle; a workspace run always wins.
    if handle is None and workspace is None and _find_workspace_from_cwd() is None:
        handle = detach_records.newest_unit()

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
    prog, has_eventlog = _read_progress(run_dir)
    done, total, running, elapsed = _progress_counts(prog, has_eventlog)
    unit_log = _unit_log(unit) if unit else None
    log = unit_log if unit else (str(run_dir / "kas.log") if run_dir else None)
    record = StatusRecord(
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
    return _Resolved(
        record=record,
        progress=prog,
        has_eventlog=has_eventlog,
        run_dir=run_dir,
        unit_log=Path(unit_log) if unit_log else None,
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
    return _check_remote_version(host, version, local=rd._local_bakar_version())


def _version_base(version: str) -> str:
    """``bakar 0.39.0`` from ``bakar 0.39.0 (8c25fbd4a655)``: the version without its content hash."""
    return version.split(" (", 1)[0].strip()


def _check_remote_version(host: str, remote: str | None, *, local: str | None) -> int | None:
    """None when the remote bakar can answer a read-only query, else the exit status.

    These commands only read run records, so a content-hash difference between
    two builds of the same version warns and continues. A different version
    refuses: the record layout may differ.
    """
    if not local or not remote or remote == local:
        return None
    if _version_base(remote) == _version_base(local):
        print(
            f"bakar: {host} runs {remote!r} but this bakar is {local!r}: same version, different build; "
            "continuing because this query only reads run records",
            file=sys.stderr,
        )
        return None
    print(
        f"remote preflight failed for {host}: bakar version mismatch (local {local!r}, remote {remote!r}); "
        "install a matching bakar there",
        file=sys.stderr,
    )
    return EXIT_REFUSED


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


def _status_record(stdout: str) -> dict[str, Any] | None:
    """The one-object JSON status record ``stdout`` holds, or None when it is anything else."""
    try:
        obj = json.loads(stdout)
    except ValueError:
        return None
    return obj if isinstance(obj, dict) and isinstance(obj.get("state"), str) else None


def _unusable_record(host: str, proc: subprocess.CompletedProcess[str]) -> None:
    print(
        f"bakar wait: {host} returned no parseable status record (remote exit status {proc.returncode}); "
        "the outcome is unknown",
        file=sys.stderr,
    )
    if proc.stderr:
        print(proc.stderr, end="", file=sys.stderr)


def run_on_host(
    host: str,
    command: str,
    options: list[str],
    handle: str | None = None,
    *,
    workspace: Path | None = None,
    capture_timeout: float | None,
    transport_exit: int,
    status_record: bool = False,
    show_stdout: bool = True,
) -> int:
    """Run ``bakar <command> <options> [-- <handle>]`` on ``host`` and mirror its exit status.

    The command goes over stdin to ``bash -s`` (never as an ssh argument) with
    every word shlex-quoted, and ``--`` precedes ``handle`` so a handle starting
    with ``-`` cannot become a flag. ``workspace`` is refused here because it
    names a local path. A failed transport (ssh exit 255, which includes a
    timed-out capture) returns ``transport_exit`` and names the ssh status.
    Only the status-record commands (status, wait) have a ``host`` field to
    fill from ``--on``.

    ``status_record`` is for ``wait``: its outcome rides on the remote exit
    status, which a crashed remote (a traceback exits 1) would otherwise forge
    into ``failed``. The remote must print a JSON record with a ``state``;
    without one the result is ``transport_exit`` (unknown), except that remote
    status 2 (handle not resolved) carries no record and passes through, and
    remote status 1 is only believed when the record says ``failed``.
    ``show_stdout`` False keeps that record off the local stdout.
    """
    from bakar.steps import remote_dispatch as rd

    if workspace is not None:
        print(f"bakar {command}: -w/--workspace cannot be combined with --on", file=sys.stderr)
        return EXIT_REFUSED
    refused = _preflight(host, transport_exit=transport_exit)
    if refused is not None:
        return refused
    words = ["bakar", command, *options]
    if handle is not None:
        words += ["--", handle]
    proc = rd._ssh_bash_capture(host, shlex.join(words) + "\n", timeout=capture_timeout)  # type: ignore[arg-type]
    if proc.returncode == SSH_TRANSPORT_FAILURE:
        _report_transport(host, proc)
        return transport_exit
    if status_record and proc.returncode != EXIT_REFUSED:
        record = _status_record(proc.stdout)
        if record is None or (proc.returncode == 1 and record["state"] != "failed"):
            _unusable_record(host, proc)
            return transport_exit
    if proc.stdout and show_stdout:
        fill = command in ("status", "wait") and "--json" in options
        print(_fill_host(proc.stdout, host) if fill else proc.stdout, end="")
    if proc.stderr:
        print(proc.stderr, end="", file=sys.stderr)
    return proc.returncode


def stream_on_host(host: str, command: str, options: list[str], *, transport_exit: int) -> int:
    """Run ``bakar <command> <options>`` on ``host`` and stream its stdout until it ends.

    The streaming twin of :func:`run_on_host`, for a command that runs until
    interrupted (``bakar log``). The same preflight runs first, the command goes
    over stdin to ``bash -s`` with every word shlex-quoted, and a failed
    transport (ssh exit 255) returns ``transport_exit``. A ``KeyboardInterrupt``
    propagates to the caller after the ssh child is stopped.
    """
    from bakar.steps import remote_dispatch as rd

    refused = _preflight(host, transport_exit=transport_exit)
    if refused is not None:
        return refused
    code = rd.stream_bash_script(host, shlex.join(["bakar", command, *options]) + "\n")
    if code == SSH_TRANSPORT_FAILURE:
        print(f"bakar {command}: ssh to {host} failed (exit {SSH_TRANSPORT_FAILURE})", file=sys.stderr)
        return transport_exit
    return code


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
    detail: Annotated[
        bool,
        typer.Option("--detail", help="Add running and failed tasks, the last log line, disk, memory and pressure."),
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
        options = ["--json"] if json_out else []
        if detail:
            options.append("--detail")
        raise typer.Exit(
            code=run_on_host(
                on,
                "status",
                options,
                handle,
                workspace=workspace,
                capture_timeout=REMOTE_STATUS_TIMEOUT,
                transport_exit=2,
            )
        )
    resolved = _resolve(handle, workspace)
    if resolved is None:
        console.print(f"No run found for {handle or 'the current workspace'}", markup=False, highlight=False)
        raise typer.Exit(code=2)
    rec = resolved.record
    if not detail:
        if json_out:
            print(json.dumps(rec.to_dict()))
            return
        console.print(render_summary(rec), markup=False, highlight=False)
        return
    snapshot = build_detail(
        state=rec.state,
        run_dir=resolved.run_dir,
        progress=resolved.progress,
        has_eventlog=resolved.has_eventlog,
        unit_log=resolved.unit_log,
        now=time.time(),
    )
    if json_out:
        print(json.dumps({**rec.to_dict(), "detail": snapshot}))
        return
    for line in detail_lines(render_summary(rec), snapshot, running_total=rec.tasks_running):
        console.print(line, markup=False, highlight=False, soft_wrap=True)
