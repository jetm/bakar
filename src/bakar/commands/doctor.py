"""bakar doctor subcommand - pre-flight checks."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer

import bakar.commands._app as _state
from bakar.commands._app import app
from bakar.commands._helpers import (
    _bbsetup_workspace,
    _normalize_dispatch,
    _print_diagnosis,
    _resolve_workspace,
)
from bakar.config import BSPSpec, resolve
from bakar.diagnostics import any_blocking_failure, run_all


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
    output_json: Annotated[
        bool,
        typer.Option("--json", "-j", help="Output results as JSON instead of formatted table."),
    ] = False,
) -> None:
    """Run every diagnostic check and exit non-zero on BLOCK failures."""
    setup_dir = _bbsetup_workspace(workspace) if kas_yaml is None and manifest is None else None
    if setup_dir is not None:
        cfg = resolve(
            workspace=setup_dir,
            bsp_family="bbsetup",
            user_config=_state._USER_CONFIG,
        )
        results = run_all(cfg, None)
        if output_json:
            _print_json(results)
        else:
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
    if output_json:
        _print_json(results)
    else:
        _print_diagnosis(results)
    if any_blocking_failure(results):
        raise typer.Exit(code=2)


def _print_json(results: list) -> None:
    doc = {
        "version": 1,
        "findings": [
            {
                "check": r.name,
                "severity": r.severity,
                "status": r.status,
                "message": r.message,
                "fix_hint": r.fix_hint,
            }
            for r in results
        ],
    }
    typer.echo(json.dumps(doc, indent=2))
