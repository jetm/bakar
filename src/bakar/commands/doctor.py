"""bakar doctor subcommand - pre-flight checks."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Annotated

import typer
from rich.table import Table

import bakar.commands._app as _state
from bakar.commands._app import app, console
from bakar.commands._helpers import (
    _bbsetup_workspace,
    _normalize_dispatch,
    _print_diagnosis,
    _resolve_workspace,
)
from bakar.config import BSPSpec, resolve
from bakar.diagnostics import any_blocking_failure, run_all
from bakar.psi import psi_recommendation, read_psi_avg10


def _run_psi_calibrate() -> None:
    """Monitor /proc/pressure/ during a running build and print config recommendations.

    Always raises typer.Exit(0) -- never returns normally.
    """
    if read_psi_avg10("cpu") is None:
        console.print("[yellow]PSI not available on this kernel (/proc/pressure/ unreadable)[/]")
        raise typer.Exit(0)
    dims = ("cpu", "io", "memory")
    peaks: dict[str, float] = dict.fromkeys(dims, 0.0)
    console.print("[bold]Monitoring /proc/pressure/ - run your build now. Press Ctrl+C to stop.[/]")
    try:
        while True:
            table = Table(title="PSI avg10 (current / peak)", show_header=True, show_edge=False)
            table.add_column("Dimension")
            table.add_column("Current")
            table.add_column("Peak")
            for dim in dims:
                current = read_psi_avg10(dim)
                if current is not None and current > peaks[dim]:
                    peaks[dim] = current
                table.add_row(dim, f"{current:.2f}" if current is not None else "n/a", f"{peaks[dim]:.2f}")
            console.clear()
            console.print(table)
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    rec = psi_recommendation(peaks)
    console.print("\n[bold]Recommended [build] block for ~/.config/bakar/config.toml:[/]")
    console.print("[build]")
    for dim in dims:
        console.print(f"pressure_max_{dim} = {rec[dim]}")
    raise typer.Exit(0)


@app.command()
def doctor(
    kas_yaml: Annotated[
        Path | None,
        typer.Argument(
            exists=False,
            help="Optional kas YAML; when supplied, BSP family is inferred from it instead of --manifest.",
        ),
    ] = None,
    manifest: Annotated[
        str | None,
        typer.Option("--manifest", "-f", help="Manifest filename used to dispatch BSP family"),
    ] = None,
    workspace: Annotated[
        Path | None,
        typer.Option("--workspace", "-w", help="Workspace root; auto-detected if omitted"),
    ] = None,
    psi_calibrate: Annotated[
        bool,
        typer.Option(
            "--psi-calibrate",
            "-C",
            help=(
                "Monitor /proc/pressure/ during a running build and print recommended "
                "pressure_max_* values for config.toml. Press Ctrl+C to stop and print results."
            ),
        ),
    ] = False,
) -> None:
    """Run every diagnostic check and exit non-zero on BLOCK failures."""
    if psi_calibrate:
        _run_psi_calibrate()

    setup_dir = _bbsetup_workspace(workspace) if kas_yaml is None and manifest is None else None
    if setup_dir is not None:
        cfg = resolve(
            workspace=setup_dir,
            bsp_family="bbsetup",
            user_config=_state._USER_CONFIG,
        )
        results = run_all(cfg, None)
        _print_diagnosis(results)
        if any_blocking_failure(results):
            raise typer.Exit(code=2)
        return

    family, bsp, kas_yaml, manifest = _normalize_dispatch(kas_yaml, manifest)
    cfg = resolve(
        workspace=_resolve_workspace(workspace, kas_yaml=kas_yaml, family=family),
        bsp_family=family,
        spec=BSPSpec(manifest=manifest),
        kas_yaml=kas_yaml,
        user_config=_state._USER_CONFIG,
    )
    results = run_all(cfg, bsp)
    _print_diagnosis(results)
    if any_blocking_failure(results):
        raise typer.Exit(code=2)
