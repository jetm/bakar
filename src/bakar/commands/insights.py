"""bakar insights subcommand - per-recipe/per-task analytics for a completed run.

Renders four independently-selectable report sections computed by
:mod:`bakar.insights_sstate`, :mod:`bakar.insights_timing`,
:mod:`bakar.insights_pressure`, and :mod:`bakar.insights_disk` from a
resolved run directory's persisted artifacts:

- ``--sstate``: per-recipe sstate cache hit/miss breakdown.
- ``--timing``: per-task wall-clock duration, top-N slowest tasks, and a
  best-effort critical-path section (unavailable here - see below).
- ``--pressure``: PSI CPU/IO/memory time-share summary with a verdict.
- ``--disk``: per-run disk-usage growth with an optional threshold warning.

With no flags, all four sections render. Run-dir selection mirrors
``bakar report``: an explicit run ID argument, or the latest run under the
resolved workspace's search roots when omitted - never an aggregate across
multiple runs, so a ``--preset`` multi-release build's ``bakar insights`` (no
selector) targets and names exactly one run.

The critical-path sub-section of the timing report requires a live
``bitbake -g <recipe>`` invocation inside kas-container (see
``commands/graph.py`` and design.md's "Critical-path computation" decision) -
it is not a pure function over a persisted run directory alone, and which
recipe to graph is not knowable from a bare run directory. This command
therefore never supplies a ``dependency_source`` to
:func:`bakar.insights_timing.timing_report`; the critical-path section always
renders as an explicit "unavailable" note rather than attempting a live
invocation, while the duration and top-N-slowest sections still render fully.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Literal

import typer

from bakar import eventlog
from bakar.commands._app import app, console
from bakar.commands._helpers import (
    WorkspaceOption,
    _bbsetup_workspace,
    _find_run,
    _workspace_from_cwd,
)
from bakar.insights_disk import disk_report
from bakar.insights_pressure import pressure_report
from bakar.insights_sstate import sstate_report
from bakar.insights_timing import timing_report
from bakar.observability import RunLogger

_SIZE_SUFFIXES: dict[str, int] = {
    "b": 1,
    "kb": 1024,
    "mb": 1024**2,
    "gb": 1024**3,
    "tb": 1024**4,
    "k": 1024,
    "m": 1024**2,
    "g": 1024**3,
    "t": 1024**4,
}


def _parse_size_bytes(raw: str) -> int:
    """Parse a human-readable size like ``"5GB"`` or ``"512"`` into bytes.

    A bare integer (no suffix) is interpreted as already being in bytes.
    Suffixes are case-insensitive and use binary (1024-based) multiples,
    matching how disk sizes are commonly reported (5GB = 5 * 1024**3 bytes).
    Raises :class:`typer.BadParameter` for an unparseable value.
    """
    text = raw.strip()
    for suffix, multiplier in sorted(_SIZE_SUFFIXES.items(), key=lambda pair: -len(pair[0])):
        if text.lower().endswith(suffix) and text[: -len(suffix)].strip():
            number = text[: -len(suffix)].strip()
            try:
                return int(float(number) * multiplier)
            except ValueError:
                break
    try:
        return int(text)
    except ValueError:
        raise typer.BadParameter(f"could not parse size: {raw!r} (expected e.g. '5GB' or a byte count)") from None


def _load_json_list(path: Path) -> list[dict]:
    """Read ``path`` as a JSON list, returning ``[]`` when absent or malformed."""
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return []
    return data if isinstance(data, list) else []


def _render_sstate(report) -> None:
    console.print("[bold]sstate:[/]")
    if report.message is not None:
        console.print(f"  {report.message}")
        return
    for stat in report.recipes:
        console.print(f"  {stat.recipe}: {stat.hits} hits, {stat.misses} misses, {stat.miss_ratio * 100:.1f}% miss")


def _render_timing(report) -> None:
    console.print("[bold]timing:[/]")
    if not report.top_slowest:
        console.print("  no timing data found for run")
    for d in report.top_slowest:
        baseline = ""
        if d.baseline_mean is not None:
            baseline = f" (baseline mean {d.baseline_mean:.1f}s)"
        console.print(f"  {d.recipe}:{d.task}: {d.duration:.1f}s{baseline}")
    cp = report.critical_path
    console.print("[bold]critical path:[/]")
    if cp.available:
        console.print(f"  {' -> '.join(cp.chain)} ({cp.total_seconds:.1f}s)")
    else:
        console.print(f"  {cp.note}")


def _render_pressure(report) -> None:
    console.print("[bold]pressure:[/]")
    if not report.available:
        console.print(f"  {report.verdict}")
        return
    for dim, pct in report.time_share.items():
        console.print(f"  {dim}: {pct:.1f}%")
    console.print(f"  verdict: {report.verdict}")


def _render_disk(report) -> None:
    console.print("[bold]disk:[/]")
    if report.message is not None:
        console.print(f"  {report.message}")
    elif report.growth_bytes is not None:
        console.print(f"  growth: {report.growth_bytes} bytes")
    if report.warning is not None:
        console.print(f"  [yellow]{report.warning}[/]")
    for event in report.full_events:
        console.print(f"  [red]disk full:[/] {event}")


@app.command("insights")
def insights(
    run_id: Annotated[
        str | None,
        typer.Argument(help="Run ID (YYYYMMDD-HHMMSS). Latest if omitted."),
    ] = None,
    manifest: Annotated[
        str | None,
        typer.Option("--manifest", "-f", help="Manifest filename used to dispatch BSP family"),
    ] = None,
    workspace: WorkspaceOption = None,
    show_sstate: Annotated[
        bool,
        typer.Option("--sstate", help="Show the per-recipe sstate hit/miss report."),
    ] = False,
    show_timing: Annotated[
        bool,
        typer.Option("--timing", help="Show the per-task timing and top-N-slowest report."),
    ] = False,
    show_pressure: Annotated[
        bool,
        typer.Option("--pressure", help="Show the PSI CPU/IO/memory pressure report."),
    ] = False,
    show_disk: Annotated[
        bool,
        typer.Option("--disk", help="Show the disk-usage growth report."),
    ] = False,
    top: Annotated[
        int,
        typer.Option("--top", help="Number of slowest tasks to show in the timing report."),
    ] = 10,
    growth_threshold: Annotated[
        str | None,
        typer.Option("--growth-threshold", help="Warn when disk growth exceeds this size (e.g. 5GB)."),
    ] = None,
) -> None:
    """Render per-recipe/per-task analytics for a completed run.

    Reads the resolved run's normalized event artifact (and, for
    ``--pressure``/``--disk``, its persisted PSI/disk-sample sibling files)
    and prints the requested sections. With no ``--sstate``/``--timing``/
    ``--pressure``/``--disk`` flag, all four sections render. Selects the
    latest run under the workspace's search roots unless an explicit run ID
    is given, and always names the run it reported on.
    """
    runs_dirs: list[tuple[Path, Literal["nxp", "ti", "generic"]]]
    if (setup_dir := _bbsetup_workspace(workspace)) is not None:
        runs_dirs = [(setup_dir / "build" / "runs", "generic")]
        not_found_label = f"{runs_dirs[0][0]}"
    else:
        ws = workspace or _workspace_from_cwd()
        runs_dirs = [
            (ws / "nxp" / "build" / "runs", "nxp"),
            (ws / "ti" / "build" / "runs", "ti"),
            (ws / "build" / "runs", "generic"),
        ]
        for build_dir in sorted(ws.glob("build-*")):
            if build_dir.is_dir():
                runs_dirs.append((build_dir / "build" / "runs", "generic"))
        not_found_label = "nxp/build/runs/, ti/build/runs/, or build/runs/"

    found = _find_run(runs_dirs, run_id)
    if found is None:
        if run_id:
            console.print(f"[red]Run {run_id} not found under {not_found_label}[/]")
        else:
            console.print(f"[yellow]No runs found under {not_found_label}.[/]")
        raise typer.Exit(code=1)

    run_dir, _label = found
    log = RunLogger(runs_dir=run_dir.parent, run_id=run_dir.name)

    threshold_bytes = _parse_size_bytes(growth_threshold) if growth_threshold is not None else None

    show_all = not (show_sstate or show_timing or show_pressure or show_disk)

    console.print(f"[bold]:: insights {log.run_id}[/]")

    if show_sstate or show_all:
        artifact = eventlog.normalize(log.eventlog_path)
        _render_sstate(sstate_report(artifact))

    if show_timing or show_all:
        artifact = eventlog.normalize(log.eventlog_path)
        _render_timing(timing_report(artifact, top_n=top))

    if show_pressure or show_all:
        psi_samples = _load_json_list(log.psi_samples_path)
        _render_pressure(pressure_report(psi_samples))

    if show_disk or show_all:
        artifact = eventlog.normalize(log.eventlog_path)
        disk_samples = _load_json_list(log.disk_samples_path)
        _render_disk(disk_report(disk_samples, artifact, threshold_bytes=threshold_bytes))
