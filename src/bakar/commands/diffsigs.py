"""bakar diffsigs subcommand - why did this task rebuild?

Runs ``bitbake -S printdiff <recipe>`` inside kas-container to generate
sigdata, then ``bitbake-diffsigs -t <recipe> <task>`` to render the
per-variable old-vs-new differences. Requires a prior build so the
reference sigdata files exist.
"""

from __future__ import annotations

import shlex
from pathlib import Path
from typing import Annotated

import typer

import bakar.commands._app as _state
from bakar.commands._app import app, console
from bakar.commands._helpers import (
    _dispatch_bsp,
    _overlay_for,
    _resolve_workspace,
)
from bakar.config import BSPSpec, resolve
from bakar.observability import RunLogger
from bakar.steps.kas_build import KasBuildContext, run_shell_capture


@app.command("diffsigs")
def diffsigs(
    recipe: Annotated[
        str,
        typer.Argument(help="Recipe name to inspect (e.g. busybox, core-image-minimal)."),
    ],
    task: Annotated[
        str,
        typer.Argument(help="Task name to inspect (e.g. do_compile, do_fetch)."),
    ],
    manifest: Annotated[
        str | None,
        typer.Option("--manifest", "-f", help="Manifest filename used to dispatch BSP family"),
    ] = None,
    machine: Annotated[
        str | None,
        typer.Option("--machine", "-m", help="Override the target machine"),
    ] = None,
    workspace: Annotated[
        Path | None,
        typer.Option("--workspace", "-w", help="Workspace root override"),
    ] = None,
) -> None:
    """Show why a task missed sstate and rebuilt.

    Runs ``bitbake -S printdiff <recipe>`` to generate sigdata, then
    ``bitbake-diffsigs -t <recipe> <task>`` to render per-variable
    old-vs-new differences. Requires a prior build so the reference
    sigdata files exist.

    When no prior sigdata is found, exits non-zero with a clear message
    rather than printing an empty diff.
    """
    family, bsp = _dispatch_bsp(manifest)
    ws = _resolve_workspace(workspace, family=family)
    cfg = resolve(
        workspace=ws,
        bsp_family=family,
        spec=BSPSpec(manifest=manifest, machine=machine),
        user_config=_state._USER_CONFIG,
    )
    overlay_source = _overlay_for(bsp)
    cfg.runs_dir.mkdir(parents=True, exist_ok=True)

    with RunLogger(runs_dir=cfg.runs_dir) as log:
        kas_ctx = KasBuildContext(cfg, log, cfg.kas_yaml, overlay_source)

        # Step 1: generate sigdata by running bitbake with the printdiff signature handler.
        printdiff_out = log.run_dir / "diffsigs-printdiff.log"
        rc_printdiff = run_shell_capture(
            kas_ctx,
            f"bitbake -S printdiff {shlex.quote(recipe)}",
            printdiff_out,
            step="diffsigs_printdiff",
        )
        if rc_printdiff != 0:
            console.print(
                f"[red]bitbake -S printdiff {recipe} failed (exit {rc_printdiff}).[/]\n"
                "Check that the workspace is synced and the recipe name is correct."
            )
            raise typer.Exit(code=rc_printdiff)

        # Step 2: render the per-variable differences between sigdata files.
        diffsigs_out = log.run_dir / "diffsigs-render.log"
        rc_diffsigs = run_shell_capture(
            kas_ctx,
            f"bitbake-diffsigs -t {shlex.quote(recipe)} {shlex.quote(task)}",
            diffsigs_out,
            step="diffsigs_render",
        )

        if rc_diffsigs != 0:
            # Distinguish missing-sigdata from other errors by inspecting output.
            raw = diffsigs_out.read_text(errors="replace") if diffsigs_out.exists() else ""
            missing_sigdata = "No such file" in raw or not raw.strip()
            if missing_sigdata:
                console.print(
                    f"[red]Required sigdata for {recipe}:{task} does not exist.[/]\n"
                    "Run a build first so bitbake writes the reference sigdata stamps,\n"
                    "then re-run: bakar diffsigs"
                )
            else:
                console.print(f"[red]bitbake-diffsigs failed (exit {rc_diffsigs}).[/]\n{raw}")
            raise typer.Exit(code=rc_diffsigs)

        diff_text = diffsigs_out.read_text(errors="replace") if diffsigs_out.exists() else ""
        console.print(f"[bold]diffsigs:[/] {recipe} {task}")
        console.print(diff_text)
