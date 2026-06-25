"""bakar sync subcommand - manifest-driven source sync without building."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Annotated

import typer

import bakar.commands._app as _state
from bakar.commands._app import app, console
from bakar.commands._helpers import (
    _bbsetup_workspace,
    _clean_build_dir,
    _dispatch_bsp,
    _overlay_for,
    _print_layer_hashes,
    _run_doctor_gate,
    _tuning_extra_overlays,
    _workspace_from_cwd,
)
from bakar.config import DEFAULT_CONTAINER_IMAGE, BSPSpec, resolve
from bakar.observability import RunLogger
from bakar.workspace import detect


def _print_dry_run(cfg, family) -> None:
    """Print the sync commands that would run as structured ``key: value`` lines.

    Reflects the actual resolved manifest/branch/paths so the preview matches
    what a real sync would invoke. NXP checks workspace state and emits only
    the steps that a real sync would execute (``repo init`` is skipped when
    the workspace is already initialised with the right manifest/branch); TI
    emits the ``oe-layertool-setup.sh`` invocation unconditionally.
    """
    if family == "nxp":
        state = detect(cfg)
        nproc = os.environ.get("NPROC", str(os.cpu_count() or 8))
        sync_cmd = f"repo sync -j {nproc} --force-sync --no-clone-bundle"
        if state.needs_full_reinit:
            init = f"repo init -u {cfg.repo_url} -b {cfg.repo_branch} -m {cfg.manifest} --config-name"
            command = f"{init} && {sync_cmd}"
        elif state.needs_repo_sync:
            command = sync_cmd
        else:
            command = "(already synced)"
    else:
        from bakar.steps.ti_layertool import _build_layertool_cmd

        command = " ".join(_build_layertool_cmd(cfg))
    print(f"command: {command}")


def _run_sync_body(cfg, log, *, bsp, family, effective_show_layers) -> None:
    """Execute the sync steps inside an active RunLogger context."""
    _run_doctor_gate(cfg, log, bsp)

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
    clean: Annotated[
        bool,
        typer.Option("--clean", help="Remove <bsp>/build/ before syncing."),
    ] = False,
    workspace: Annotated[Path | None, typer.Option("--workspace", "-w", help="Workspace root override")] = None,
    show_layers: Annotated[
        bool,
        typer.Option("--show-layers", help="Print layer git hashes after sync."),
    ] = False,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", "-n", help="Print the sync commands that would run, then exit without syncing."),
    ] = False,
    dry_run_script: Annotated[
        str | None,
        typer.Option(
            "--dry-run-script",
            help="Write a runnable bash script to PATH (or stdout when PATH is '-') and exit without syncing.",
            metavar="PATH",
        ),
    ] = None,
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
        spec=BSPSpec(machine=machine, distro=distro, image=image, manifest=manifest, repo_branch=branch),
        user_config=_state._USER_CONFIG,
    )

    if dry_run:
        _print_dry_run(cfg, family)
        raise typer.Exit(code=0)

    if dry_run_script is not None:
        from bakar.steps.kas_build import generate_dry_run_script

        overlay_source = _overlay_for(bsp)
        extra_overlays = _tuning_extra_overlays(cfg)
        try:
            script = generate_dry_run_script(
                cfg,
                cfg.kas_yaml,
                overlay_source,
                extra_overlays,
                generating_command="bakar sync --dry-run-script",
            )
        except ValueError as exc:
            console.print(f"[red]Cannot generate dry-run script:[/] {exc}")
            raise typer.Exit(code=2) from None
        if dry_run_script == "-":
            sys.stdout.write(script)
        else:
            Path(dry_run_script).write_text(script, encoding="utf-8")
        raise typer.Exit(code=0)

    if "KAS_CONTAINER_IMAGE" not in os.environ and cfg.kas_container_image != DEFAULT_CONTAINER_IMAGE:
        console.print(f"[dim]container image from config: {cfg.kas_container_image}[/]")

    effective_show_layers = show_layers or (_state._USER_CONFIG is not None and _state._USER_CONFIG.show_hashes)

    console.print(f"[bold]::[/] bakar sync [{family}] manifest={cfg.manifest}")

    if clean:
        _clean_build_dir(cfg)

    cfg.runs_dir.mkdir(parents=True, exist_ok=True)
    with RunLogger(runs_dir=cfg.runs_dir) as log:
        _run_sync_body(cfg, log, bsp=bsp, family=family, effective_show_layers=effective_show_layers)
    console.print("[bold green]sync complete[/]")
