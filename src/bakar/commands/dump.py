"""bakar dump subcommand - flatten the kas YAML and overlays into resolved output."""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Annotated

import typer

import bakar.commands._app as _state
from bakar.commands._app import app, console
from bakar.commands._helpers import (
    _combine_overlays_with_tuning,
    _dispatch_bsp,
    _dispatch_from_yaml,
    _overlay_for,
    _resolve_workspace,
    split_kas_yaml_arg,
)
from bakar.config import BSPSpec, resolve
from bakar.observability import RunLogger
from bakar.steps import kas_build as step_kas
from bakar.steps.kas_build import KasBuildContext


@app.command("dump")
def dump(
    kas_yaml: Annotated[
        str | None,
        typer.Argument(
            help="Optional kas YAML (BYO); supports colon-overlay syntax: machine.yml:overlay.yml.",
        ),
    ] = None,
    manifest: Annotated[
        str | None,
        typer.Option("--manifest", "-f", help="Manifest filename used to dispatch BSP family"),
    ] = None,
    workspace: Annotated[
        Path | None,
        typer.Option("--workspace", "-w", help="Workspace root override"),
    ] = None,
    output: Annotated[
        Path | None,
        typer.Option(
            "--output",
            "-o",
            help="Write the resolved YAML to this path instead of stdout.",
        ),
    ] = None,
) -> None:
    """Flatten the build kas YAML plus tuning overlay into a single resolved YAML.

    Runs ``kas dump`` on the build-YAML-plus-overlay argument, honoring
    container-vs-host mode. With no ``--output`` the resolved YAML is printed
    to stdout; otherwise it is written to the given path.
    """
    if kas_yaml is not None and manifest is not None:
        console.print("[red]choose either a positional kas YAML or --manifest, not both[/]")
        raise typer.Exit(code=2)

    main_yaml, user_extras = split_kas_yaml_arg(kas_yaml)

    if main_yaml is not None:
        family, bsp = _dispatch_from_yaml(main_yaml)
    else:
        family, bsp = _dispatch_bsp(manifest)

    ws = _resolve_workspace(workspace, kas_yaml=main_yaml, family=family)
    cfg = resolve(
        workspace=ws,
        bsp_family=family,
        spec=BSPSpec(manifest=manifest),
        kas_yaml=main_yaml,
        user_config=_state._USER_CONFIG,
    )
    overlay_source = _overlay_for(bsp)
    extra_overlays = _combine_overlays_with_tuning(user_extras, cfg)
    # dump is not a build: use an ephemeral run dir so it does not leave a
    # bogus build/runs/<ts>/ entry that `report`/`triage` would surface.
    with tempfile.TemporaryDirectory() as runs_tmp, RunLogger(runs_dir=Path(runs_tmp)) as log:
        kas_ctx = KasBuildContext(cfg, log, cfg.kas_yaml, overlay_source, extra_overlays=extra_overlays)
        try:
            rc = step_kas.run_kas_subcommand(
                kas_ctx,
                "dump",
                [],
                capture_to=output,
            )
        except FileNotFoundError:
            exe = "kas" if cfg.host_mode else "kas-container"
            console.print(f"[red]{exe} not found[/]; pass --host to use plain kas, or install kas-container")
            raise typer.Exit(code=2) from None
    raise typer.Exit(code=rc)
