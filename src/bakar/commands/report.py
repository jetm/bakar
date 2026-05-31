"""bakar report subcommand - success-path summary of a completed build run."""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Annotated, Literal

import typer

import bakar.commands._app as _state
from bakar.commands._app import app, console
from bakar.commands._helpers import _bbsetup_workspace, _find_run, _print_layer_hashes, _workspace_from_cwd
from bakar.config import BSPSpec, resolve
from bakar.report import assemble_report


@app.command("report")
def report(
    run_id: Annotated[
        str | None,
        typer.Argument(help="Run ID (YYYYMMDD-HHMMSS). Latest if omitted."),
    ] = None,
    manifest: Annotated[
        str | None,
        typer.Option("--manifest", "-f", help="Manifest filename used to dispatch BSP family"),
    ] = None,
    workspace: Annotated[
        Path | None,
        typer.Option("--workspace", "-w", help="Workspace root override"),
    ] = None,
    json_out: Annotated[
        bool,
        typer.Option("--json", help="Emit the summary as a single JSON object on stdout."),
    ] = False,
) -> None:
    """Summarize a completed build run from its structured logs.

    Reads the resolved run's ``events.jsonl``, ``du.tsv``, and layer git
    state and prints the run id, build status, duration, deploy directory and
    image size, peak build-tmp size, and per-layer SHAs. With ``--json`` the
    same fields are emitted as one JSON object. Kernel version and recipe
    count are best-effort and omitted when unavailable.
    """
    family: Literal["nxp", "ti", "generic", "bbsetup"]
    if (setup_dir := _bbsetup_workspace(workspace)) is not None:
        runs_dirs: list[tuple[Path, Literal["nxp", "ti", "generic"]]] = [
            (setup_dir / "build" / "runs", "generic"),
        ]
        not_found_label = f"{runs_dirs[0][0]}"
        ws_for_cfg = setup_dir
        family = "bbsetup"
    else:
        ws = workspace or _workspace_from_cwd()
        runs_dirs = [
            (ws / "nxp" / "build" / "runs", "nxp"),
            (ws / "ti" / "build" / "runs", "ti"),
            (ws / "build" / "runs", "generic"),
        ]
        not_found_label = "nxp/build/runs/, ti/build/runs/, or build/runs/"
        ws_for_cfg = ws
        family = "nxp"  # provisional default; overwritten by the resolved run's label below

    found = _find_run(runs_dirs, run_id)
    if found is None:
        if run_id:
            console.print(f"[red]Run {run_id} not found under {not_found_label}[/]")
        else:
            console.print(f"[yellow]No runs found under {not_found_label}.[/]")
        raise typer.Exit(code=1)

    run_dir, label = found
    if family != "bbsetup":
        family = label
    cfg = resolve(
        workspace=ws_for_cfg,
        bsp_family=family,
        spec=BSPSpec(manifest=manifest),
        user_config=_state._USER_CONFIG,
    )

    summary = assemble_report(run_dir, cfg)

    if json_out:
        payload = {
            "run_id": summary.run_id,
            "status": summary.status,
            "duration_s": summary.duration_s,
            "deploy_dir": summary.deploy_dir,
            "image_size": summary.image_size,
            "peak_tmp_bytes": summary.peak_tmp_bytes,
            "layers": [dataclasses.asdict(layer) for layer in summary.layers],
        }
        print(json.dumps(payload))
        return

    console.print(f"[bold]::[/] report {summary.run_id}")
    status_colour = "green" if summary.status == "success" else "red"
    console.print(f"status: [{status_colour}]{summary.status}[/]")
    if summary.duration_s is not None:
        console.print(f"duration: {summary.duration_s:.0f}s")
    if summary.deploy_dir:
        console.print(f"deploy: {summary.deploy_dir}")
    if summary.image_size is not None:
        console.print(f"image size: {summary.image_size} bytes")
    if summary.peak_tmp_bytes is not None:
        console.print(f"peak build/tmp: {summary.peak_tmp_bytes} bytes")
    _print_layer_hashes(cfg, hashes=summary.layers)
