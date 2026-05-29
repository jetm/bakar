"""bakar clean subcommand - wipe the BSP build directory."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Literal

import typer

import bakar.commands._app as _state
from bakar.commands._app import app, console
from bakar.commands._helpers import (
    _bsp_from_cwd,
    _clean_build_dir,
    _dispatch_bsp,
    _workspace_from_cwd,
)
from bakar.config import resolve


@app.command()
def clean(
    all: Annotated[bool, typer.Option("--all", help="Also remove the generated kas YAML")] = False,
    bsp: Annotated[
        str | None,
        typer.Option("--bsp", help="BSP family to clean: 'nxp' or 'ti'. Auto-detected from cwd if omitted."),
    ] = None,
    manifest: Annotated[
        str | None,
        typer.Option("--manifest", "-f", help="Manifest filename (back-compat alias for --bsp)"),
    ] = None,
    workspace: Annotated[Path | None, typer.Option("--workspace", "-w", help="Workspace root override")] = None,
) -> None:
    """Remove the BSP-specific build/ directory. Use --all to also drop the kas YAML."""
    ws = workspace or _workspace_from_cwd()

    family: Literal["nxp", "ti"] | None = None
    if bsp is not None:
        if bsp not in ("nxp", "ti"):
            console.print(f"[red]invalid --bsp value[/]: {bsp!r} (expected 'nxp' or 'ti')")
            raise typer.Exit(code=2)
        family = bsp  # type: ignore[assignment]
    elif manifest is not None:
        family, _bsp_model = _dispatch_bsp(manifest)
    else:
        family = _bsp_from_cwd(ws)
        if family is None:
            console.print("[red]could not auto-detect BSP from cwd. Pass --bsp nxp|ti or --manifest <file>.[/]")
            raise typer.Exit(code=2)
    cfg = resolve(workspace=ws, bsp_family=family, user_config=_state._USER_CONFIG)
    if all:
        # Stop the workspace hashserv daemon before wiping so it isn't
        # orphaned pointing at a removed working directory. Lazy import to
        # avoid any future import cycle if hashserv grows transitive deps.
        from bakar import hashserv

        hashserv.stop(cfg.bsp_root)
    _clean_build_dir(cfg)
    if all and cfg.kas_yaml.exists():
        cfg.kas_yaml.unlink()
        console.print(f"[green]removed[/] {cfg.kas_yaml}")
