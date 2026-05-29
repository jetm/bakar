"""bspctl lock subcommand - pin floating layer SHAs to exact commits."""

from __future__ import annotations

import subprocess
import tempfile
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
from bspctl.steps.kas_build import run_kas_subcommand


@app.command("lock")
def lock(
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
    output: Annotated[
        Path | None,
        typer.Option(
            "--output",
            "-o",
            help="Write the pinned manifest here instead of the default location (NXP only).",
        ),
    ] = None,
) -> None:
    """Pin every floating layer revision to an exact commit.

    NXP manifest workspaces wrap ``repo manifest -r`` to write a SHA-pinned
    manifest XML (to ``--output`` when given, else ``<bsp_root>/pinned-manifest.xml``).
    BYO and bbsetup/TI workspaces wrap ``kas lock`` (``kas-container lock``
    outside host mode) to produce a ``kas-project.lock.yml`` lockfile.
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
        workspace=ws, bsp_family=family, manifest=manifest, kas_yaml=kas_yaml, user_config=_state._USER_CONFIG
    )

    if bsp is not None and bsp.manifest_kind == "repo-xml":
        out = (output if output is not None else cfg.bsp_root / "pinned-manifest.xml").resolve()
        out.parent.mkdir(parents=True, exist_ok=True)
        proc = subprocess.run(
            ["repo", "manifest", "-r", "-o", str(out)],
            cwd=cfg.workspace / "nxp",
        )
        raise typer.Exit(code=proc.returncode)

    overlay_source = _overlay_for(bsp)
    # lock is not a build: use an ephemeral run dir so it does not leave a
    # bogus build/runs/<ts>/ entry that `report`/`triage` would surface.
    with tempfile.TemporaryDirectory() as runs_tmp, RunLogger(runs_dir=Path(runs_tmp)) as log:
        try:
            rc = run_kas_subcommand(
                cfg,
                log,
                "lock",
                [],
                kas_yaml=cfg.kas_yaml,
                overlay_source=overlay_source,
            )
        except FileNotFoundError:
            exe = "kas" if cfg.host_mode else "kas-container"
            console.print(f"[red]{exe} not found[/]; pass --host to use plain kas, or install kas-container")
            raise typer.Exit(code=2) from None
    raise typer.Exit(code=rc)
