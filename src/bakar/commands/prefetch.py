"""bakar prefetch subcommand - pre-fetch recipe sources without building."""

from __future__ import annotations

import shlex
from pathlib import Path
from typing import Annotated

import typer

import bakar.commands._app as _state
from bakar.commands._app import app, console
from bakar.commands._helpers import (
    WorkspaceOption,
    _bbsetup_workspace,
    _normalize_dispatch,
    _overlay_for,
    _resolve_workspace,
)
from bakar.config import BSPSpec, resolve
from bakar.kas import write_bbsetup_yaml
from bakar.observability import RunLogger
from bakar.steps import kas_build as step_kas
from bakar.steps.kas_build import KasBuildContext


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
    image: Annotated[
        str | None,
        typer.Option("--image", "-i", help="Fetch target; defaults to core-image-minimal when unset"),
    ] = None,
    workspace: WorkspaceOption = None,
) -> None:
    """Pre-fetch all recipe sources into DL_DIR without running the build.

    Runs ``bitbake --runall=fetch <image>`` inside the kas environment so
    every recipe's source downloads populate ``DL_DIR`` ahead of an
    offline build. Uses ``kas-container`` by default and plain ``kas``
    when host mode is active, consistent with the build pipeline.
    """
    setup_dir = _bbsetup_workspace(workspace) if kas_yaml is None and manifest is None else None
    if setup_dir is not None:
        cfg = resolve(
            workspace=setup_dir,
            bsp_family="bbsetup",
            spec=BSPSpec(machine=machine, image=image),
            user_config=_state._USER_CONFIG,
        )
        # Value-based, matching build.py's guard: an explicit --image/BAKAR_IMAGE
        # override must still win, not just the literal "generic"/"" sentinel
        # default that resolve() falls back to for the bbsetup family.
        fetch_target = cfg.image if cfg.image not in ("", "generic") else "core-image-minimal"
        try:
            write_bbsetup_yaml(setup_dir, target=fetch_target, machine_override=machine, distro_override=None)
        except ValueError as exc:
            console.print(f"[red]bitbake-setup config error:[/] {exc}")
            raise typer.Exit(code=2) from exc
        overlay_source = _overlay_for(None)
    else:
        family, bsp, kas_yaml, manifest = _normalize_dispatch(kas_yaml, manifest)
        ws = _resolve_workspace(workspace, kas_yaml=kas_yaml, family=family)
        cfg = resolve(
            workspace=ws,
            bsp_family=family,
            spec=BSPSpec(machine=machine, manifest=manifest, image=image),
            kas_yaml=kas_yaml,
            user_config=_state._USER_CONFIG,
        )
        fetch_target = cfg.image if cfg.image not in ("", "generic") else "core-image-minimal"
        overlay_source = _overlay_for(bsp)

    cfg.runs_dir.mkdir(parents=True, exist_ok=True)
    with RunLogger(runs_dir=cfg.runs_dir) as log:
        kas_ctx = KasBuildContext(cfg, log, cfg.kas_yaml, overlay_source)
        rc = step_kas.run_shell(
            kas_ctx,
            [],
            command=f"bitbake --runall=fetch {shlex.quote(fetch_target)}",
        )
    raise typer.Exit(code=rc)
