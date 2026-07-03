"""bakar log subcommand - tail a run's log file."""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Annotated

import typer

import bakar.commands._app as _state
from bakar.commands._app import app, console
from bakar.commands._helpers import (
    WorkspaceOption,
    _dispatch_bsp,
    _dispatch_from_yaml,
    _resolve_workspace,
)
from bakar.config import BSPSpec, resolve

_LOG_FILES: dict[str, str] = {
    "kas": "kas.log",
    "console": "console.log",
    "events": "events.jsonl",
}


def _tail_follow(path: Path, history_lines: int = 40) -> None:
    """Pure-Python `tail -f`: print the last N lines, then stream new content.

    Seeking straight to EOF hides everything already written to the log,
    so if the build is between writes when the user opens the tail, the
    screen stays blank. Emit a chunk of recent history first (matches
    `tail -f` default behavior) so `bakar log` is useful mid-run.
    """
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        lines = fh.readlines()
        for line in lines[-history_lines:]:
            sys.stdout.write(line)
        sys.stdout.flush()
        while True:
            line = fh.readline()
            if line:
                sys.stdout.write(line)
                sys.stdout.flush()
            else:
                time.sleep(0.2)


def _resolve_run_dir(runs_dir: Path, run: str | None) -> Path:
    """Return the run directory for the given run ID, or the latest run if None.

    Raises typer.Exit(code=1) when no runs exist or the requested ID is not found.
    """
    run_dirs = sorted(
        (p for p in runs_dir.iterdir() if p.is_dir()),
        key=lambda p: p.name,
    )
    if not run_dirs:
        console.print("[red]no runs yet[/]; start one with `bakar build`")
        raise typer.Exit(code=1)
    if run is None:
        return run_dirs[-1]
    run_dir = runs_dir / run
    if not run_dir.is_dir():
        console.print(f"[red]run directory not found[/]: {run_dir}")
        raise typer.Exit(code=1)
    return run_dir


@app.command("log")
def log_cmd(
    kas_yaml: Annotated[
        Path | None,
        typer.Argument(
            exists=False,
            help="Optional kas YAML; runs live next to it under <yaml-parent>/build/runs/.",
        ),
    ] = None,
    run: Annotated[
        str | None,
        typer.Option("--run", help="Run ID (YYYYMMDD-HHMMSS). Latest if omitted."),
    ] = None,
    which: Annotated[
        str,
        typer.Option("--which", help="Which log to follow: kas, console, or events."),
    ] = "kas",
    manifest: Annotated[
        str | None,
        typer.Option("--manifest", "-f", help="Manifest filename used to dispatch BSP family"),
    ] = None,
    workspace: WorkspaceOption = None,
) -> None:
    """Tail the latest bakar run's kas.log live. Use --run for a specific run, --which to pick a different log file.

    Pass a positional kas YAML for BYO builds (``bakar log my.yml``);
    runs live next to the YAML under ``<yaml-parent>/build/runs/`` and
    the workspace lookup is skipped.
    """
    if which not in _LOG_FILES:
        console.print(f"[red]invalid --which value[/]: {which!r} (expected one of: kas, console, events)")
        raise typer.Exit(code=2)

    if kas_yaml is not None and manifest is not None:
        console.print("[red]choose either a positional kas YAML or --manifest, not both[/]")
        raise typer.Exit(code=2)

    if kas_yaml is not None:
        family, _bsp = _dispatch_from_yaml(kas_yaml)
    else:
        family, _bsp = _dispatch_bsp(manifest)
    ws = _resolve_workspace(workspace, kas_yaml=kas_yaml, family=family)
    cfg = resolve(
        workspace=ws,
        bsp_family=family,
        spec=BSPSpec(manifest=manifest),
        kas_yaml=kas_yaml,
        user_config=_state._USER_CONFIG,
    )
    runs_dir = cfg.runs_dir
    if not runs_dir.is_dir():
        console.print("[red]no runs yet[/]; start one with `bakar build`")
        raise typer.Exit(code=1)

    run_dir = _resolve_run_dir(runs_dir, run)

    log_name = _LOG_FILES[which]
    target = run_dir / log_name
    if not target.is_file():
        if which == "kas":
            fallback = run_dir / _LOG_FILES["console"]
            if fallback.is_file():
                console.print(
                    f"[yellow]note:[/] {log_name} not present yet (build hasn't reached "
                    f"kas_build); falling back to {fallback.name}"
                )
                target = fallback
            else:
                console.print(f"[red]no kas.log or console.log in[/] {run_dir}")
                raise typer.Exit(code=1)
        else:
            console.print(f"[red]log file not found[/]: {target}")
            raise typer.Exit(code=1)

    console.print(f"[dim]following[/] {target}")
    try:
        _tail_follow(target)
    except KeyboardInterrupt:
        raise typer.Exit(code=0) from None
