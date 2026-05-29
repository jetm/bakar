"""bspctl layers subcommand - display synced layer git state."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import bspctl.commands._app as _state
import typer
from bspctl.commands._app import app, console
from bspctl.commands._helpers import (
    _dispatch_bsp,
    _dispatch_from_yaml,
    _print_layer_hashes,
    _resolve_workspace,
)
from bspctl.config import resolve
from bspctl.layers import collect_layer_hashes


@app.command("layers")
def layers(
    kas_yaml: Annotated[
        Path | None,
        typer.Argument(
            exists=False,
            help="Optional kas YAML (BYO); resolves the workspace next to it.",
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
) -> None:
    """Print each synced layer's repo name, git short-hash, and branch.

    Read-only: collects layer git state once and never triggers a build,
    sync, or any workspace write.
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
        workspace=ws, bsp_family=family, manifest=manifest, kas_yaml=kas_yaml, user_config=_state._USER_CONFIG
    )

    hashes = collect_layer_hashes(cfg)
    if not hashes:
        console.print("no layers yet; run `bspctl build` or `bspctl sync` first")
        raise typer.Exit(code=0)

    _print_layer_hashes(cfg, hashes=hashes)
