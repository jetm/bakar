"""bspctl prefetch subcommand - pre-fetch recipe sources without building."""

from __future__ import annotations

import shlex
from pathlib import Path
from typing import Annotated

import bspctl.commands._app as _state
import typer
from bspctl.commands._app import app, console
from bspctl.commands._helpers import (
    _dispatch_bsp,
    _dispatch_from_yaml,
    _overlay_for,
    _resolve_workspace,
)
from bspctl.config import resolve
from bspctl.observability import RunLogger
from bspctl.steps import kas_build as step_kas


@app.command()
def prefetch(
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
    machine: Annotated[
        str | None,
        typer.Option("--machine", "-m", help="e.g. imx8mp-var-dart, am62x-var-som"),
    ] = None,
    workspace: Annotated[Path | None, typer.Option("--workspace", "-w", help="Workspace root override")] = None,
) -> None:
    """Pre-fetch all recipe sources into DL_DIR without running the build.

    Runs ``bitbake --runall=fetch <image>`` inside the kas environment so
    every recipe's source downloads populate ``DL_DIR`` ahead of an
    offline build. Uses ``kas-container`` by default and plain ``kas``
    when host mode is active, consistent with the build pipeline.
    """
    if kas_yaml is not None and manifest is not None:
        console.print("[red]choose either a positional kas YAML or --manifest, not both[/]")
        raise typer.Exit(code=2)

    if kas_yaml is not None:
        family, bsp = _dispatch_from_yaml(kas_yaml)
    else:
        family, bsp = _dispatch_bsp(manifest)

    ws = _resolve_workspace(workspace, kas_yaml=kas_yaml, family=family)
    cfg = resolve(
        workspace=ws,
        bsp_family=family,
        machine=machine,
        manifest=manifest,
        kas_yaml=kas_yaml,
        user_config=_state._USER_CONFIG,
    )
    overlay_source = _overlay_for(bsp)
    cfg.runs_dir.mkdir(parents=True, exist_ok=True)
    with RunLogger(runs_dir=cfg.runs_dir) as log:
        rc = step_kas.run_shell(
            cfg,
            log,
            [],
            command=f"bitbake --runall=fetch {shlex.quote(cfg.image)}",
            kas_yaml=cfg.kas_yaml,
            overlay_source=overlay_source,
        )
    raise typer.Exit(code=rc)
