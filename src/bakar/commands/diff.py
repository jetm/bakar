"""bakar diff subcommand - compare two manifest/config versions."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Annotated

import typer

import bakar.commands._app as _state
from bakar.commands._app import app, console
from bakar.commands._helpers import _dispatch_bsp, _resolve_workspace
from bakar.config import BSPSpec, resolve
from bakar.manifest_diff import diff_manifests


@app.command("diff")
def diff(
    old: Annotated[
        Path,
        typer.Argument(help="Old manifest XML (NXP) or kas config (BYO)."),
    ],
    new: Annotated[
        Path,
        typer.Argument(help="New manifest XML (NXP) or kas config (BYO)."),
    ],
    manifest: Annotated[
        str | None,
        typer.Option("--manifest", "-f", help="Manifest filename used to dispatch BSP family"),
    ] = None,
    workspace: Annotated[Path | None, typer.Option("--workspace", "-w", help="Workspace root override")] = None,
) -> None:
    """Compare two versions and report per-layer SHA changes.

    When both arguments are ``repo`` manifest XMLs (``.xml``), each layer's
    old and new SHA are diffed and rendered with a best-effort commit count
    and a changed/unchanged marker. Otherwise the arguments are treated as
    kas config files (BYO/bbsetup) and the command delegates to ``kas diff``
    (``kas-container diff`` outside host mode).
    """
    family, _bsp = _dispatch_bsp(manifest)
    ws = _resolve_workspace(workspace, family=family)
    cfg = resolve(
        workspace=ws,
        bsp_family=family,
        spec=BSPSpec(manifest=manifest),
        user_config=_state._USER_CONFIG,
    )

    if old.suffix == ".xml" and new.suffix == ".xml":
        diffs = diff_manifests(old, new, checkout_root=cfg.bsp_root / "sources")
        for d in diffs:
            old_col = d.old_sha[:8] if d.old_sha is not None else "-"
            new_col = d.new_sha[:8] if d.new_sha is not None else "-"
            count_col = f"+{d.commit_count}" if d.commit_count is not None else ""
            marker = "unchanged" if d.old_sha == d.new_sha else "changed"
            console.print(f"{d.layer}  {old_col}  {new_col}  {count_col}  {marker}")
        raise typer.Exit(code=0)

    exe = "kas" if cfg.host_mode else "kas-container"
    proc = subprocess.run([exe, "diff", str(old), str(new)], cwd=ws, check=False)
    raise typer.Exit(code=proc.returncode)
