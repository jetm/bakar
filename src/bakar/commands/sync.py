"""bakar sync subcommand - manifest-driven source sync without building."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated

import typer

import bakar.commands._app as _state
from bakar.commands._app import app, console
from bakar.commands._helpers import (
    _bbsetup_workspace,
    _clean_build_dir,
    _dispatch_bsp,
    _print_diagnosis,
    _print_layer_hashes,
    _workspace_from_cwd,
)
from bakar.config import DEFAULT_CONTAINER_IMAGE, resolve
from bakar.diagnostics import any_blocking_failure, run_all
from bakar.observability import RunLogger
from bakar.workspace import detect


def _run_sync_body(cfg, log, *, bsp, family, effective_show_layers, skip_doctor) -> None:
    """Execute the sync steps inside an active RunLogger context."""
    run_doctor = not skip_doctor and (_state._USER_CONFIG is None or _state._USER_CONFIG.doctor)
    if run_doctor:
        log.step_start("doctor")
        results = run_all(cfg, bsp)
        _print_diagnosis(results)
        if any_blocking_failure(results):
            log.step_fail("doctor", reason="blocking failure")
            raise typer.Exit(code=2)
        log.step_ok("doctor", checks=len(results))

    state = detect(cfg)
    if state.needs_repo_sync:
        reasons: list[str] = []
        if state.repo_broken:
            reasons.append(".repo/ broken")
        if state.manifest_mismatch:
            reasons.append(f"manifest {state.repo_manifest_include!r} -> {cfg.manifest!r}")
        if state.branch_mismatch:
            reasons.append(f"branch {state.repo_manifests_branch!r} -> {cfg.repo_branch!r}")
        if state.sha_drift:
            reasons.append(f"{len(state.sha_drift)} pinned SHA drift")
        if reasons:
            console.print("[yellow]manifest drift:[/] " + "; ".join(reasons) + " - forcing full re-sync")
        bsp.sync_step(cfg, log, force_init=state.needs_full_reinit)
    else:
        log.step_skip(
            "repo_sync" if family == "nxp" else "ti_layertool",
            reason="already synced",
        )

    state = detect(cfg)
    if state.needs_setup_env:
        bsp.setup_env_step(cfg, log)
    else:
        log.step_skip("setup_env", reason="bblayers.conf present")

    if effective_show_layers:
        _print_layer_hashes(cfg)


@app.command()
def sync(
    machine: Annotated[str | None, typer.Option("--machine", "-m")] = None,
    distro: Annotated[str | None, typer.Option("--distro", "-d")] = None,
    image: Annotated[str | None, typer.Option("--image", "-i")] = None,
    manifest: Annotated[
        str | None,
        typer.Option(
            "--manifest",
            "-f",
            help="manifest filename (NXP imx-*.xml or TI processor-sdk-*-config_var<N>.txt)",
        ),
    ] = None,
    branch: Annotated[
        str | None,
        typer.Option(
            "--branch",
            "-b",
            help="branch override; inferred from manifest filename when omitted",
        ),
    ] = None,
    skip_doctor: Annotated[
        bool,
        typer.Option("--skip-doctor", help="Skip the pre-flight diagnosis (not recommended)"),
    ] = False,
    clean: Annotated[
        bool,
        typer.Option("--clean", help="Remove <bsp>/build/ before syncing."),
    ] = False,
    workspace: Annotated[Path | None, typer.Option("--workspace", "-w", help="Workspace root override")] = None,
    show_layers: Annotated[
        bool,
        typer.Option("--show-layers", help="Print layer git hashes after sync."),
    ] = False,
) -> None:
    """Run the manifest-driven sync without building.

    Equivalent to the first half of ``bakar build``: doctor, then
    repo init+sync (NXP) or oe-layertool populate (TI), then var-setup-release
    or local.conf fixup. Useful when you want to refresh ``sources/``
    without kicking off a kas-container build.

    bitbake-setup workspaces are initialized externally via
    ``bitbake-setup init``; ``bakar sync`` fails fast for them.
    """
    if _bbsetup_workspace(workspace) is not None:
        console.print(
            "[red]bitbake-setup workspaces are initialized with `bitbake-setup init`[/] - run that first, then retry"
        )
        raise typer.Exit(code=2)

    family, bsp = _dispatch_bsp(manifest)
    ws = workspace or _workspace_from_cwd()
    cfg = resolve(
        workspace=ws,
        bsp_family=family,
        machine=machine,
        distro=distro,
        image=image,
        manifest=manifest,
        repo_branch=branch,
        user_config=_state._USER_CONFIG,
    )

    if "KAS_CONTAINER_IMAGE" not in os.environ and cfg.container_image != DEFAULT_CONTAINER_IMAGE:
        console.print(f"[dim]container image from config.toml: {cfg.container_image}[/]")

    effective_show_layers = show_layers or (_state._USER_CONFIG is not None and _state._USER_CONFIG.show_hashes)

    console.print(f"[bold]::[/] bakar sync [{family}] manifest={cfg.manifest}")

    if clean:
        _clean_build_dir(cfg)

    cfg.runs_dir.mkdir(parents=True, exist_ok=True)
    with RunLogger(runs_dir=cfg.runs_dir) as log:
        _run_sync_body(
            cfg, log, bsp=bsp, family=family, effective_show_layers=effective_show_layers, skip_doctor=skip_doctor
        )
    console.print("[bold green]sync complete[/]")
