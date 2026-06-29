"""bakar stress-parse subcommand - bitbake parser fork-race stress test."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated

import typer
from rich.table import Table

import bakar.commands._app as _state
from bakar.commands._app import app, console
from bakar.commands._helpers import (
    _dispatch_bsp,
    _overlay_for,
    _workspace_from_cwd,
    global_container_mode,
    global_host_mode,
)
from bakar.config import BSPSpec, resolve
from bakar.observability import RunLogger
from bakar.steps import bitbake_override as step_override
from bakar.steps import kas_build as step_kas
from bakar.steps import stress_parse as step_stress_parse


@app.command("stress-parse")
def stress_parse(
    runs: Annotated[
        int,
        typer.Option("--runs", "-n", help="Number of bitbake -p iterations to run."),
    ] = 10,
    target: Annotated[
        str,
        typer.Option(
            "--target",
            help="bitbake parse target. Default 'world' parses every recipe in the layer graph.",
        ),
    ] = "world",
    parse_threads: Annotated[
        int | None,
        typer.Option(
            "--parse-threads",
            help="Override BB_NUMBER_PARSE_THREADS for each iteration. "
            "Use 1 to confirm the serial baseline; raise above nproc to amplify the race.",
        ),
    ] = None,
    machine: Annotated[str | None, typer.Option("--machine", "-m")] = None,
    image: Annotated[str | None, typer.Option("--image", "-i")] = None,
    manifest: Annotated[
        str | None,
        typer.Option("--manifest", "-f", help="Manifest filename used to dispatch BSP family."),
    ] = None,
    branch: Annotated[
        str | None,
        typer.Option("--branch", "-b", help="Branch override; inferred from manifest when omitted."),
    ] = None,
    workspace: Annotated[Path | None, typer.Option("--workspace", "-w")] = None,
    label: Annotated[
        str | None,
        typer.Option(
            "--label",
            help="Free-form tag persisted into summary.json for cross-run aggregation. "
            "Used by the patch-ablation matrix and CPython version sweep to group runs.",
        ),
    ] = None,
    python: Annotated[
        Path | None,
        typer.Option(
            "--python",
            help="Override which Python bitbake re-execs into. Sets BB_PYTHON3 and "
            "leads PATH with the interpreter's bin dir, so stress-parse can validate "
            "a locally-built CPython (e.g. one carrying an obmalloc fork-"
            "safety patch) without reinstalling bakar under that interpreter. Pass "
            "the absolute path to a python binary; defaults to bakar's own interpreter.",
        ),
    ] = None,
) -> None:
    """Stress-test the bitbake parser fork-race fix with N parse-only iterations.

    Loops ``bitbake -p <target>`` inside ``kas-container`` (or directly on
    the host when ``--host`` is given) and scans each iteration's output
    for the canonical fork-race signatures. Writes per-iteration logs
    and a ``summary.json`` under
    ``<bsp>/build/runs/<run-id>/stress-parse/``. Exits non-zero if any
    iteration tripped a signature.

    Assumes the workspace is already synced (run ``bakar build`` once
    first); applies the bitbake override and regenerates the kas YAML
    before looping. Skips the doctor pre-flight - the user opts into
    stress-parse explicitly.
    """
    if runs < 1:
        console.print("[red]--runs must be >= 1[/]")
        raise typer.Exit(code=2)

    # --host / --container are global callback options; read into locals used below.
    host_mode = global_host_mode()
    container_mode = global_container_mode()

    python_executable: Path | None = None
    if python is not None:
        python_executable = python.expanduser().resolve()
        if not python_executable.is_file() or not os.access(python_executable, os.X_OK):
            console.print(f"[red]--python: not an executable file: {python_executable}[/]")
            raise typer.Exit(code=2)

    family, bsp = _dispatch_bsp(manifest)
    ws = workspace or _workspace_from_cwd()
    cfg = resolve(
        workspace=ws,
        bsp_family=family,
        spec=BSPSpec(
            machine=machine,
            image=image,
            manifest=manifest,
            repo_branch=branch,
            host_mode=host_mode,
            container_mode=container_mode,
        ),
        user_config=_state._USER_CONFIG,
    )
    overlay_source = _overlay_for(bsp)

    console.print(
        f"[bold]::[/] bakar stress-parse [{family}] {cfg.machine} / {cfg.image} "
        f"target={target} runs={runs}"
        + (f" parse-threads={parse_threads}" if parse_threads is not None else "")
        + (" [host-mode]" if host_mode else "")
        + (f" label={label!r}" if label else "")
        + (f" python={python_executable}" if python_executable is not None else "")
    )

    cfg.runs_dir.mkdir(parents=True, exist_ok=True)
    with RunLogger(runs_dir=cfg.runs_dir) as log:
        step_override.apply(cfg, log)
        step_kas.regenerate_yaml(cfg, log, bsp=bsp)
        summary = step_stress_parse.run(
            cfg,
            log,
            bsp=bsp,
            overlay_source=overlay_source,
            runs=runs,
            target=target,
            parse_threads=parse_threads,
            label=label,
            python_executable=python_executable,
        )

    table = Table(title=f"stress-parse summary ({family})", show_edge=False)
    table.add_column("metric", no_wrap=True)
    table.add_column("value")
    table.add_row("runs", str(summary["runs"]))
    table.add_row("passed", f"[green]{summary['passed']}[/]")
    fail_colour = "red" if summary["failed"] else "dim"
    table.add_row("failed", f"[{fail_colour}]{summary['failed']}[/]")
    if summary["elapsed_seconds"]:
        avg = sum(summary["elapsed_seconds"]) / len(summary["elapsed_seconds"])
        table.add_row("avg seconds", f"{avg:.1f}")
    if summary["override"]:
        ov = summary["override"]
        table.add_row(
            "override",
            f"{ov.get('state')} branch={ov.get('branch')} sha={ov.get('sha')}",
        )
    console.print(table)

    if summary["failed"] > 0:
        for sig in summary["failure_signatures"][:10]:
            console.print(f"[red]run {sig['run']}[/] [{sig['pattern']}] {sig['match']}")
        if len(summary["failure_signatures"]) > 10:
            console.print(f"[dim]... {len(summary['failure_signatures']) - 10} more matches truncated[/]")
        raise typer.Exit(code=1)
