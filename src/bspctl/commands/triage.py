"""bspctl triage subcommand - post-mortem the last build run."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Annotated, Literal

import typer

from bspctl.bsp_detect import detect_kas_workspace, is_meta_avocado_yaml
from bspctl.commands._app import app, console
from bspctl.commands._helpers import _bbsetup_workspace, _find_run, _workspace_from_cwd
from bspctl.triage import analyse


@app.command()
def triage(
    run_id: Annotated[str | None, typer.Argument(help="Run ID (YYYYMMDD-HHMMSS). Latest if omitted.")] = None,
    kas_yaml: Annotated[
        Path | None,
        typer.Option(
            "--kas-yaml",
            "-k",
            help="kas YAML for a BYO build; runs live next to it under <yaml-parent>/build/runs/.",
        ),
    ] = None,
    workspace: Annotated[Path | None, typer.Option("--workspace", "-w", help="Workspace root override")] = None,
) -> None:
    """Surface the last failed step of the named run (or the most recent).

    Without ``--kas-yaml`` searches both ``nxp/build/runs/`` and
    ``ti/build/runs/`` under the workspace. Pass ``--kas-yaml my.yml``
    for a BYO build whose runs live next to the YAML
    (``<yaml-parent>/build/runs/``); the BSP family is inferred from
    the run directory's location and reported as ``generic`` for
    generic BYO YAMLs.
    """
    if kas_yaml is not None:
        resolved = kas_yaml.resolve()
        if is_meta_avocado_yaml(resolved):
            build_root = detect_kas_workspace(resolved) / f"build-{resolved.stem}"
        else:
            build_root = resolved.parent
        runs_dirs: list[tuple[Path, Literal["nxp", "ti", "generic"]]] = [
            (build_root / "build" / "runs", "generic"),
        ]
        report_root = build_root
        not_found_label = f"{runs_dirs[0][0]}"
    elif (setup_dir := _bbsetup_workspace(workspace)) is not None:
        runs_dirs = [(setup_dir / "build" / "runs", "generic")]
        report_root = setup_dir
        not_found_label = f"{runs_dirs[0][0]}"
    else:
        ws = workspace or _workspace_from_cwd()
        runs_dirs = [
            (ws / "nxp" / "build" / "runs", "nxp"),
            (ws / "ti" / "build" / "runs", "ti"),
        ]
        report_root = ws
        not_found_label = "nxp/build/runs/ or ti/build/runs/"

    found = _find_run(runs_dirs, run_id)
    if found is None:
        if run_id:
            console.print(f"[red]Run {run_id} not found under {not_found_label}[/]")
        else:
            console.print(f"[yellow]No runs found under {not_found_label}.[/]")
        raise typer.Exit(code=1)

    run_dir, _label = found
    report = analyse(run_dir, report_root)
    console.print(f"[bold]::[/] triage {run_dir.name}")
    if report.failing_step:
        console.print(f"[red]✗[/] step [bold]{report.failing_step}[/] failed: {report.fail_reason}")
    else:
        console.print("[green]no step_fail events found[/]")

    if report.kas_log_tail:
        console.print("[dim]kas.log (tail):[/]")
        for line in report.kas_log_tail:
            sys.stdout.write(f"  {line.rstrip()}\n")
        sys.stdout.flush()
    if report.recipe_log:
        console.print(f"[bold]bitbake recipe log:[/] {report.recipe_log}")
        if report.recipe_log_tail:
            console.print(f"[dim]{report.recipe_log.name} (tail):[/]")
            for line in report.recipe_log_tail:
                sys.stdout.write(f"  {line.rstrip()}\n")
            sys.stdout.flush()
    if report.suggestions:
        console.print("[cyan]suggestions:[/]")
        for s in report.suggestions:
            console.print(f"  - {s}")
