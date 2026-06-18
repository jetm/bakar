"""bakar stop subcommand - gracefully halt a running build."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

import bakar.commands._app as _state
from bakar import build_stop
from bakar.commands._app import app, console
from bakar.commands._helpers import (
    _dispatch_bsp,
    _dispatch_from_yaml,
    _resolve_workspace,
)
from bakar.config import BSPSpec, resolve


@app.command("stop")
def stop(
    kas_yaml: Annotated[
        Path | None,
        typer.Argument(
            exists=False,
            help="Optional kas YAML; runs live next to it under <yaml-parent>/build/runs/.",
        ),
    ] = None,
    workspace: Annotated[Path | None, typer.Option("--workspace", "-w", help="Workspace root override")] = None,
    manifest: Annotated[
        str | None,
        typer.Option("--manifest", "-f", help="Manifest filename used to resolve the BSP family"),
    ] = None,
    force: Annotated[
        bool,
        typer.Option("--force", help="Skip the SIGINT grace period and escalate straight to SIGTERM"),
    ] = False,
) -> None:
    """Gracefully stop the running build for this workspace's BSP.

    Pass a positional kas YAML for BYO builds (``bakar stop my.yml``);
    runs live next to the YAML under ``<yaml-parent>/build/runs/`` and
    the workspace lookup is skipped.
    """
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
    build_stop.stop_build(cfg.bsp_root, force=force)
