"""bakar lock subcommand - pin floating layer SHAs to exact commits."""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path
from typing import Annotated

import typer

import bakar.commands._app as _state
from bakar.commands._app import app, console
from bakar.commands._helpers import (
    WorkspaceOption,
    _normalize_dispatch,
    _overlay_for,
    _resolve_workspace,
)
from bakar.config import BSPSpec, resolve
from bakar.observability import RunLogger
from bakar.steps import kas_build as step_kas
from bakar.steps.kas_build import KasBuildContext


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
    workspace: WorkspaceOption = None,
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
    family, bsp, kas_yaml, manifest = _normalize_dispatch(kas_yaml, manifest)
    ws = _resolve_workspace(workspace, kas_yaml=kas_yaml, family=family)
    cfg = resolve(
        workspace=ws,
        bsp_family=family,
        spec=BSPSpec(manifest=manifest),
        kas_yaml=kas_yaml,
        user_config=_state._USER_CONFIG,
    )

    if bsp is not None and bsp.manifest_kind == "repo-xml":
        out = (output if output is not None else cfg.bsp_root / "pinned-manifest.xml").resolve()
        out.parent.mkdir(parents=True, exist_ok=True)
        proc = subprocess.run(
            ["repo", "manifest", "-r", "-o", str(out)],
            cwd=cfg.bsp_root,
            check=False,
        )
        raise typer.Exit(code=proc.returncode)

    overlay_source = _overlay_for(bsp)
    # lock is not a build: use an ephemeral run dir so it does not leave a
    # bogus build/runs/<ts>/ entry that `report`/`triage` would surface.
    with tempfile.TemporaryDirectory() as runs_tmp, RunLogger(runs_dir=Path(runs_tmp)) as log:
        kas_ctx = KasBuildContext(cfg, log, cfg.kas_yaml, overlay_source)
        try:
            rc = step_kas.run_kas_subcommand(
                kas_ctx,
                "lock",
                [],
            )
        except FileNotFoundError:
            exe = "kas" if cfg.host_mode else "kas-container"
            console.print(f"[red]{exe} not found[/]; pass --host to use plain kas, or install kas-container")
            raise typer.Exit(code=2) from None
    raise typer.Exit(code=rc)
