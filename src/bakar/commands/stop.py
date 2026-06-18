"""bakar stop subcommand - gracefully halt a running build."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Literal

import typer

import bakar.commands._app as _state
from bakar import build_stop
from bakar.commands._app import app, console
from bakar.commands._helpers import (
    _bsp_from_cwd,
    _dispatch_bsp,
    _workspace_from_cwd,
)
from bakar.config import resolve


def _resolve_family(
    manifest: str | None,
    ws: Path,
) -> Literal["nxp", "ti"]:
    """Resolve the BSP family from stop's flag ladder.

    Order: the ``--manifest`` alias dispatched through :func:`_dispatch_bsp`;
    cwd auto-detection via :func:`_bsp_from_cwd`. Any unresolvable path raises
    ``typer.Exit(code=2)`` with a hint - mirrors ``clean.py`` minus the
    ``--bsp`` branch (this command has no ``--bsp`` flag).
    """
    if manifest is not None:
        family, _bsp_model = _dispatch_bsp(manifest)
        return family
    family = _bsp_from_cwd(ws)
    if family is None:
        console.print("[red]could not auto-detect BSP from cwd. Pass --manifest <file>.[/]")
        raise typer.Exit(code=2)
    return family


@app.command("stop")
def stop(
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
    """Gracefully stop the running build for this workspace's BSP."""
    ws = workspace or _workspace_from_cwd()
    family = _resolve_family(manifest, ws)
    cfg = resolve(workspace=ws, bsp_family=family, user_config=_state._USER_CONFIG)
    build_stop.stop_build(cfg.bsp_root, force=force)
