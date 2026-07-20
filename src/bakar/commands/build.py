"""bakar build subcommand - full BSP build pipeline."""

from __future__ import annotations

import os
import shutil
import sys
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer
from rich.console import Console
from rich.table import Table

import bakar.commands._app as _state
from bakar.bsp_detect import machine_from_yaml
from bakar.commands._app import app, console
from bakar.commands._helpers import (
    WorkspaceOption,
    _bbsetup_workspace,
    _clean_build_dir,
    _combine_overlays_with_tuning,
    _dispatch_bsp,
    _dispatch_from_yaml,
    _overlay_for,
    _print_layer_hashes,
    _print_sstate_summary,
    _resolve_workspace,
    _run_doctor_gate,
    _tuning_extra_overlays,
    _uninitialized_bbsetup_dir,
    _workspace_from_cwd,
    apply_mold_overrides,
    apply_scope_override,
    global_container_mode,
    global_host_mode,
    global_output_mode_override,
    split_kas_yaml_arg,
)
from bakar.config import DEFAULT_CONTAINER_IMAGE, BSPSpec, compose_preset_output_path, resolve
from bakar.fmt import fmt_duration
from bakar.kas import translate_bbsetup_config, write_bbsetup_yaml
from bakar.observability import RunLogger
from bakar.output_mode import OutputMode, resolve_output_mode
from bakar.preset_config import load_presets
from bakar.steps import bitbake_override as step_override
from bakar.steps import kas_build as step_kas
from bakar.steps import qcom_build as step_qcom_build
from bakar.steps.kas_build import KasBuildContext
from bakar.workspace import detect

if TYPE_CHECKING:
    from bakar.bsp_model import BspModel


def _output_mode() -> OutputMode:
    """Resolve the human-output mode from the global override and this run's stream."""
    return resolve_output_mode(global_output_mode_override(), isatty=sys.stderr.isatty(), ci_env=os.environ.get("CI"))


def _plain_render_console() -> Console | None:
    """A no-color render console for RunLogger under plain mode, else None (module default).

    Keeps the out-of-Live summary/hint lines, layer tables, and alerts ANSI-free even on a
    forced ``--plain`` TTY (design D9).
    """
    if _output_mode() is OutputMode.PLAIN:
        return Console(no_color=True, force_terminal=False, stderr=True)
    return None


def _make_kas_ctx(cfg, log, overlay_source: Path, ctx) -> KasBuildContext:
    """Build a KasBuildContext for run_build, threading the shared mode config.

    ``ctx`` is either ``_BbsetupCtx`` or ``_BuildCtx``; both carry keep_going,
    dry_run, and target, the only fields callers need beyond cfg/log/overlay.
    """
    return KasBuildContext(
        cfg,
        log,
        cfg.kas_yaml,
        overlay_source,
        keep_going=ctx.keep_going,
        dry_run=ctx.dry_run,
        target=ctx.target,
        output_mode=_output_mode(),
    )


def _open_run_logger(cfg) -> RunLogger:
    """Open a RunLogger honoring the plain-mode render console override."""
    return RunLogger(runs_dir=cfg.runs_dir, render_console=_plain_render_console())


def _finish_build(cfg, log, rc: int, machine: str) -> None:
    """Shared build tail: rc check + triage hint, sstate summary, success line, artifacts path.

    ``machine`` names the deploy/images subdir - ``cfg.machine`` for byo/manifest
    builds, the bbsetup-translated machine for the bbsetup path.
    """
    if rc != 0:
        console.print(f"[red]kas-container build failed (exit {rc}).[/] Run `bakar triage {log.run_id}` for details.")
        raise typer.Exit(code=rc)
    # QLI names the build dir build-<distro> and the tmp dir tmp-glibc; every
    # other family uses build/tmp. build_dir_name is "build" off the qcom path,
    # so nxp/ti/generic/bbsetup stay byte-identical.
    tmp_name = "tmp-glibc" if cfg.bsp_family == "qcom" else "tmp"
    deploy = cfg.bsp_root / cfg.build_dir_name / tmp_name / "deploy" / "images" / machine
    if _state._USER_CONFIG is not None and _state._USER_CONFIG.show_sstate_summary:
        _print_sstate_summary(log.run_dir / "kas.log")
    console.print(f"[bold green]build succeeded[/] in {fmt_duration(time.monotonic() - log.start_monotonic)}")
    console.print(f"artifacts: {deploy}")


def _preset_completer(incomplete: str) -> list[str]:
    """Shell completion for --preset: returns preset names starting with incomplete."""
    try:
        presets = load_presets()
    except ValueError, OSError:
        return []
    return [p.name for p in presets if p.name.startswith(incomplete)]


@dataclass(frozen=True)
class _BbsetupCtx:
    """CLI flags for the bbsetup build path (resolved before cfg is available)."""

    machine: str | None
    distro: str | None
    image: str | None
    host_mode: bool
    container_mode: bool
    clean: bool
    dry_run: bool
    keep_going: bool
    show_layers: bool
    sstate_mirror: str | None
    sccache_dist: bool = False
    sccache_scheduler: str | None = None
    target: str | None = None
    dry_run_script: str | None = None


def _run_bbsetup_build(
    setup_dir: Path,
    ctx: _BbsetupCtx,
) -> None:
    """Full build pipeline for a bitbake-setup workspace.

    Factored out of ``build()`` to keep the main function readable.
    """
    cfg = resolve(
        workspace=setup_dir,
        bsp_family="bbsetup",
        spec=BSPSpec(
            machine=ctx.machine,
            distro=ctx.distro,
            image=ctx.image,
            host_mode=ctx.host_mode,
            container_mode=ctx.container_mode,
        ),
        user_config=_state._USER_CONFIG,
    )
    if ctx.sstate_mirror is not None:
        cfg = replace(cfg, sstate_mirror_url=ctx.sstate_mirror)
    if ctx.sccache_dist:
        cfg = replace(cfg, sccache_dist=True)
    if ctx.sccache_scheduler is not None:
        cfg = replace(cfg, sccache_scheduler_url=ctx.sccache_scheduler)
    cfg = apply_mold_overrides(cfg)
    cfg = apply_scope_override(cfg)
    overlay_source = _overlay_for(None)
    bb_target = cfg.image if cfg.image not in ("", "generic") else "core-image-minimal"

    try:
        translated = translate_bbsetup_config(
            setup_dir, target=bb_target, machine_override=ctx.machine, distro_override=ctx.distro
        )
    except ValueError as exc:
        console.print(f"[red]bitbake-setup config error:[/] {exc}")
        raise typer.Exit(code=2) from exc
    if translated["machine"] is None:
        console.print(
            "[red]no machine selected[/] - pass --machine or add a `machine/<name>` "
            "fragment to the bitbake-setup config"
        )
        raise typer.Exit(code=2)

    if "KAS_CONTAINER_IMAGE" not in os.environ and cfg.kas_container_image != DEFAULT_CONTAINER_IMAGE:
        console.print(f"[dim]container image from config: {cfg.kas_container_image}[/]")

    console.print(f"[bold]::[/] bakar build [bbsetup] {setup_dir}")

    if ctx.clean:
        tmp_dir = cfg.bsp_root / "build" / "tmp"
        if tmp_dir.exists():
            shutil.rmtree(tmp_dir)
            console.print(f"[green]removed[/] {tmp_dir}")

    effective_show_layers = ctx.show_layers or (_state._USER_CONFIG is not None and _state._USER_CONFIG.show_hashes)

    extra_overlays_bbsetup = _tuning_extra_overlays(cfg)

    if ctx.dry_run_script is not None:
        try:
            script = step_kas.generate_dry_run_script(
                cfg, cfg.kas_yaml, overlay_source, extra_overlays_bbsetup, keep_going=ctx.keep_going, target=ctx.target
            )
        except ValueError as exc:
            console.print(f"[red]Cannot generate dry-run script:[/] {exc}")
            raise typer.Exit(code=2) from None
        if ctx.dry_run_script == "-":
            sys.stdout.write(script)
        else:
            Path(ctx.dry_run_script).write_text(script)
        raise typer.Exit(code=0)

    if ctx.dry_run:
        # Dry-run: kas never writes build/conf/bblayers.conf, so print best-effort
        # from any pre-existing conf (same as BYO dry-run).
        if effective_show_layers:
            _print_layer_hashes(cfg)
        for line in step_kas.dry_run_preview_lines(
            cfg, cfg.kas_yaml, overlay_source, extra_overlays_bbsetup, keep_going=ctx.keep_going, target=ctx.target
        ):
            print(line)
        raise typer.Exit(code=0)

    cfg.runs_dir.mkdir(parents=True, exist_ok=True)
    with _open_run_logger(cfg) as log:
        log.info(f"build mode=bbsetup bsp=bbsetup yaml={cfg.kas_yaml} overlay={overlay_source}")

        _run_doctor_gate(cfg, log, None)

        write_bbsetup_yaml(
            setup_dir,
            target=bb_target,
            machine_override=ctx.machine,
            distro_override=ctx.distro,
        )

        kas_ctx = _make_kas_ctx(cfg, log, overlay_source, ctx)
        rc = step_kas.run_build(
            kas_ctx,
            extra_overlays=_tuning_extra_overlays(cfg),
            show_layers=effective_show_layers,
        )
        _finish_build(cfg, log, rc, translated["machine"])


@dataclass(frozen=True)
class _BuildCtx:
    """Resolved build flags for byo and manifest paths (assembled after cfg is available)."""

    overlay_source: Path
    extra_overlays: list[Path]
    bsp: BspModel | None
    family: str
    effective_show_layers: bool
    dry_run: bool
    keep_going: bool
    skip_sync: bool
    target: str | None = None


def _run_byo_build(
    cfg,
    log,
    ctx: _BuildCtx,
) -> None:
    """Build pipeline for BYO (bring-your-own kas YAML) mode.

    Called inside an active RunLogger context from ``build()``.
    """
    _run_doctor_gate(cfg, log, ctx.bsp)

    # BYO skips sync/setup-env, so kas generates bblayers.conf during run_build.
    # Layer hashes are only on disk once the build has run; only --dry-run can
    # print up front (best effort from any pre-existing conf).
    if ctx.effective_show_layers and ctx.dry_run:
        _print_layer_hashes(cfg)

    if not ctx.dry_run:
        if ctx.family == "generic":
            log.step_skip("bitbake_override", reason="generic mode")
        else:
            step_override.apply(cfg, log)

    kas_ctx = _make_kas_ctx(cfg, log, ctx.overlay_source, ctx)
    rc = step_kas.run_build(
        kas_ctx,
        extra_overlays=ctx.extra_overlays,
        show_layers=ctx.effective_show_layers and not ctx.dry_run,
    )
    _finish_build(cfg, log, rc, cfg.machine)


def _run_manifest_build(
    cfg,
    log,
    ctx: _BuildCtx,
) -> None:
    """Build pipeline for manifest-driven mode.

    Called inside an active RunLogger context from ``build()``.
    """
    _run_doctor_gate(cfg, log, ctx.bsp)

    # A dry run never reaches run_build's live layer panel; print best-effort
    # from any pre-existing bblayers.conf up front (mirrors the BYO path).
    if ctx.effective_show_layers and ctx.dry_run:
        _print_layer_hashes(cfg)

    assert ctx.bsp is not None
    state = detect(cfg)
    if state.needs_repo_sync and not ctx.skip_sync:
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
        ctx.bsp.sync_step(cfg, log, force_init=state.needs_full_reinit)
    else:
        log.step_skip(
            "repo_sync" if ctx.family in ("nxp", "qcom") else "ti_layertool",
            reason="already synced" if not ctx.skip_sync else "user skipped",
        )

    state = detect(cfg)
    if state.needs_setup_env:
        ctx.bsp.setup_env_step(cfg, log)
    else:
        log.step_skip("setup_env", reason="bblayers.conf present")

    if ctx.family == "qcom":
        # QLI is not a kas build: skip the bitbake-swap override, the kas YAML
        # regeneration, and kas run_build. Source setup-environment and run
        # bitbake directly in one bash subshell instead.
        rc = step_qcom_build.run(
            cfg,
            log,
            target=ctx.target or cfg.image,
            keep_going=ctx.keep_going,
            dry_run=ctx.dry_run,
        )
    else:
        if not ctx.dry_run:
            step_override.apply(cfg, log)
            step_kas.regenerate_yaml(cfg, log, bsp=ctx.bsp)

        kas_ctx = _make_kas_ctx(cfg, log, ctx.overlay_source, ctx)
        rc = step_kas.run_build(
            kas_ctx,
            extra_overlays=_tuning_extra_overlays(cfg),
            show_layers=ctx.effective_show_layers and not ctx.dry_run,
        )
    _finish_build(cfg, log, rc, cfg.machine)


def _is_multi_release(preset: object) -> bool:
    """Return True when a preset expands to more than one release."""
    from bakar.preset_config import PresetEntry

    if not isinstance(preset, PresetEntry):
        return False
    return len(preset.manifests) > 1 or len(preset.kas_yamls) > 1


def _run_single_preset_release(
    active_preset: object,
    spec_index: int,
    *,
    workspace_root: Path,
    machine: str | None,
    distro: str | None,
    image: str | None,
    branch: str | None,
    host_mode: bool,
    container_mode: bool,
    skip_sync: bool,
    dry_run: bool,
    keep_going: bool,
    clean: bool,
    show_layers: bool,
    sstate_mirror: str | None,
    sccache_dist: bool = False,
    sccache_scheduler: str | None = None,
    target: str | None = None,
) -> int:
    """Run the full build pipeline for one PresetSpec and return the exit code.

    Catches typer.Exit so multi-release fan-out can continue after a failed
    release without terminating the process.  Returns 0 on success, non-zero
    on failure.
    """
    from bakar.preset_config import PresetEntry, PresetSpec

    if not isinstance(active_preset, PresetEntry):
        return 1

    specs: list[PresetSpec] = active_preset.resolve()
    if spec_index >= len(specs):
        return 1
    spec = specs[spec_index]

    out_subdir = compose_preset_output_path(active_preset, spec_index)
    ws = workspace_root / "build" / out_subdir

    byo_form = spec.kas_yaml is not None
    main_yaml: Path | None
    user_extras: list[Path] = []
    if byo_form:
        main_yaml, user_extras = split_kas_yaml_arg(spec.kas_yaml)
        family, bsp = _dispatch_from_yaml(main_yaml)
    else:
        family, bsp = _dispatch_bsp(spec.manifest)
        main_yaml = None

    cfg = resolve(
        workspace=ws,
        bsp_family=family,
        spec=BSPSpec(
            machine=machine or spec.machine or (machine_from_yaml(main_yaml) if byo_form else None),
            distro=distro or spec.distro,
            image=image or spec.image,
            manifest=spec.manifest,
            repo_branch=branch or spec.branch,
            host_mode=host_mode,
            container_mode=container_mode,
        ),
        kas_yaml=main_yaml,
        user_config=_state._USER_CONFIG,
        preset=active_preset,
    )
    if sstate_mirror is not None:
        cfg = replace(cfg, sstate_mirror_url=sstate_mirror)
    if sccache_dist:
        cfg = replace(cfg, sccache_dist=True)
    if sccache_scheduler is not None:
        cfg = replace(cfg, sccache_scheduler_url=sccache_scheduler)
    cfg = apply_mold_overrides(cfg)
    cfg = apply_scope_override(cfg)

    overlay_source = _overlay_for(bsp)
    extra_overlays = _combine_overlays_with_tuning(user_extras, cfg)

    effective_show_layers = show_layers or (_state._USER_CONFIG is not None and _state._USER_CONFIG.show_hashes)

    ctx = _BuildCtx(
        overlay_source=overlay_source,
        extra_overlays=extra_overlays,
        bsp=bsp,
        family=family,
        effective_show_layers=effective_show_layers,
        dry_run=dry_run,
        keep_going=keep_going,
        skip_sync=skip_sync,
        target=target,
    )

    if clean:
        _clean_build_dir(cfg)

    cfg.runs_dir.mkdir(parents=True, exist_ok=True)
    try:
        with _open_run_logger(cfg) as log:
            log.info(
                f"build mode={'byo' if byo_form else 'manifest'} bsp={family}"
                f" yaml={cfg.kas_yaml} overlay={overlay_source}"
                f" release_index={spec_index}",
            )
            if byo_form:
                _run_byo_build(cfg, log, ctx)
            else:
                _run_manifest_build(cfg, log, ctx)
    except typer.Exit as exc:
        return exc.exit_code if exc.exit_code is not None else 1
    except Exception as exc:  # noqa: BLE001 - last-resort CLI handler; unexpected errors must not crash silently
        console.print(f"[red]release {spec_index} failed with unexpected error:[/] {exc}")
        return 1
    else:
        return 0


@app.command()
def build(
    kas_yaml: Annotated[
        str | None,
        typer.Argument(
            help="Optional kas YAML (BYO form). Colon-separated overlays are supported: "
            "main.yml:overlay.yml. When set, sync/setup-env/gen-kas are skipped.",
        ),
    ] = None,
    machine: Annotated[str | None, typer.Option("--machine", "-m", help="e.g. imx8mp-var-dart, am62x-var-som")] = None,
    distro: Annotated[str | None, typer.Option("--distro", "-d", help="e.g. fsl-imx-xwayland, arago")] = None,
    image: Annotated[
        str | None,
        typer.Option("--image", "-i", help="e.g. core-image-minimal, var-thin-image"),
    ] = None,
    target: Annotated[
        str | None,
        typer.Option(
            "--target",
            "-t",
            help="kas target override (kas build --target <TARGET>), e.g. avocado-complete; "
            "unset builds the YAML's own target",
        ),
    ] = None,
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
    skip_sync: Annotated[
        bool, typer.Option("--skip-sync", help="Skip sync (repo init+sync for NXP, oe-layertool for TI)")
    ] = False,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", "-n", help="Regenerate YAML and exit before invoking kas/kas-container build")
    ] = False,
    keep_going: Annotated[
        bool,
        typer.Option(
            "--keep-going",
            "-k",
            help="Pass -k to bitbake: continue building other targets when one fails",
        ),
    ] = False,
    clean: Annotated[
        bool,
        typer.Option(
            "--clean",
            help="Remove <bsp>/build/ before running the pipeline (forces a from-scratch build).",
        ),
    ] = False,
    workspace: WorkspaceOption = None,
    show_layers: Annotated[
        bool,
        typer.Option("--show-layers", help="Print layer git hashes before build."),
    ] = False,
    sstate_mirror: Annotated[
        str | None,
        typer.Option("--sstate-mirror", help="HTTP sstate/downloads mirror URL; enables the shared-cache overlay"),
    ] = None,
    dry_run_script: Annotated[
        str | None,
        typer.Option(
            "--dry-run-script",
            help="Write a runnable bash script reproducing this build to PATH, or to stdout when PATH is '-'. "
            "Does not build. The existing --dry-run/-n preview behavior is unchanged.",
        ),
    ] = None,
    preset: Annotated[
        str | None,
        typer.Option(
            "--preset",
            autocompletion=_preset_completer,
            help="Named preset from config.toml; additive with explicit flags (explicit flags win).",
        ),
    ] = None,
    on: Annotated[
        str | None,
        typer.Option(
            "--on",
            help="Dispatch the build to a remote host (ssh alias or user@ip) instead of building "
            "locally: mirror the working tree with rsync, run the build there, stream logs, and "
            "surface the remote run-id. Unset builds locally.",
        ),
    ] = None,
    yes: Annotated[
        bool,
        typer.Option(
            "--yes",
            "-y",
            help="Skip the rsync --delete confirmation prompt for --on dispatch (non-interactive).",
        ),
    ] = False,
) -> None:
    """Run the build pipeline idempotently.

    Two forms:

    * **BYO**: ``bakar build my.yml`` - skip sync/setup-env/gen-kas,
      apply the static tuning overlay, run kas-container. The YAML is
      classified as NXP, TI, or generic (a kas YAML that does not
      target an NXP/TI SoM). Generic mode picks
      ``bakar-tuning-generic.yml`` and skips the bitbake-override step
      since that swaps the vendor-bundled bitbake.
    * **Manifest-driven**: ``bakar build -f imx-6.12.49-2.2.0.xml -m imx95-var-dart`` -
      run sync, setup-env, gen-kas (topology-only), then apply overlay
      and build. Same flag surface as before, just with the optimization
      stack moved to the overlay file.

    The two forms are mutually exclusive: passing both a positional
    YAML and ``--manifest`` exits non-zero.
    """
    # --host / --container / --sccache-dist / --sccache-scheduler are global
    # callback options; read them into the local names the body threads through.
    host_mode = global_host_mode()
    container_mode = global_container_mode()
    sccache_dist = _state._SCCACHE_DIST
    sccache_scheduler = _state._SCCACHE_SCHEDULER

    # --on <host>: dispatch the entire build to a remote node instead of building
    # locally. Runs before any form-specific branch (preset, bbsetup, byo/manifest)
    # so it covers every build form (design A2). When --on is unset the body below
    # is byte-identical to today - no ssh/rsync is spawned.
    if on is not None:
        if dry_run or dry_run_script is not None:
            console.print("[red]--on cannot be combined with --dry-run/--dry-run-script[/]; run the dry run locally.")
            raise typer.Exit(code=2)
        from bakar.commands._helpers import invoking_cwd as _invoking_cwd

        # The invoking cwd is captured before _enter_workspace's eager -w chdir
        # (the bakar-stop-and-workspace-cwd A10 lesson), so the remote reproduces
        # PC1's path resolution exactly - not the post-chdir workspace root.
        invoking_cwd = _invoking_cwd()
        # Mirror the workspace the local build would resolve so a generic BYO YAML
        # run from outside a workspace does not exit 2, and a manifest/bbsetup run
        # mirrors the same tree the local build would.
        if kas_yaml is not None:
            _on_main_yaml, _ = split_kas_yaml_arg(kas_yaml)
            _on_family, _ = _dispatch_from_yaml(_on_main_yaml)
            ws_root = _resolve_workspace(workspace, kas_yaml=_on_main_yaml, family=_on_family)
        elif manifest is not None:
            _on_family, _ = _dispatch_bsp(manifest)
            ws_root = _resolve_workspace(workspace, family=_on_family)
        else:
            ws_root = _bbsetup_workspace(workspace) or _workspace_from_cwd()
        from bakar.steps.remote_dispatch import dispatch_remote_build

        rc = dispatch_remote_build(on, ws_root, invoking_cwd, sys.argv[1:], sccache_dist=sccache_dist, assume_yes=yes)
        raise typer.Exit(code=rc)
    # Resolve the active preset (if any) before dispatch.
    # PresetEntry is used only as a local variable type annotation.
    from bakar.preset_config import PresetEntry

    active_preset: PresetEntry | None = None
    if preset is not None:
        # Use presets already loaded at startup when available; fall back to
        # loading directly (task 6.2 wires _PRESETS; until then this fallback
        # keeps this code self-contained).
        # Check for None explicitly: _PRESETS=[] is a valid "no presets defined"
        # state and must not trigger a redundant load_presets() call.
        startup_presets = getattr(_state, "_PRESETS", None)
        loaded = startup_presets if startup_presets is not None else load_presets()
        matches = [p for p in loaded if p.name == preset]
        if not matches:
            console.print(f"[red]Preset '{preset}' not found.[/] Run `bakar presets list` to see available presets.")
            raise typer.Exit(code=1)
        active_preset = matches[0]

        # For bbsetup/generic presets, set kas_yaml from the preset (unless
        # the caller already supplied one explicitly).
        if active_preset.family in {"bbsetup", "generic"} and kas_yaml is None:
            if active_preset.kas_yaml:
                kas_yaml = active_preset.kas_yaml
            elif active_preset.kas_yamls:
                kas_yaml = active_preset.kas_yamls[0]

        # For nxp/ti presets, set manifest from the preset (unless the caller
        # already supplied one explicitly).
        if active_preset.family in {"nxp", "ti"} and manifest is None:
            if active_preset.manifest:
                manifest = active_preset.manifest
            elif active_preset.manifests:
                manifest = active_preset.manifests[0]

    # Multi-release fan-out: when a preset defines more than one release,
    # run each release sequentially, collect results, print a summary table,
    # and exit with code 1 if any release failed.
    if active_preset is not None and dry_run_script is not None and _is_multi_release(active_preset):
        console.print("[red]--dry-run-script is not supported for multi-release presets.[/]")
        raise typer.Exit(1)
    if active_preset is not None and _is_multi_release(active_preset):
        specs = active_preset.resolve()
        # bbsetup is not in _resolve_workspace's Literal type; treat it like
        # the generic/unknown case which falls back to _workspace_from_cwd().
        _rw_family = active_preset.family if active_preset.family in {"nxp", "ti", "generic"} else None
        ws_root = _resolve_workspace(workspace, kas_yaml=None, family=_rw_family)
        results: list[tuple[str, str, float]] = []
        for i in range(len(specs)):
            release_id = compose_preset_output_path(active_preset, i)
            console.print(
                f"\n[bold]::[/] bakar build [{active_preset.family}] release {i + 1}/{len(specs)}: {release_id}"
            )
            t0 = time.monotonic()
            rc = _run_single_preset_release(
                active_preset,
                i,
                workspace_root=ws_root,
                machine=machine,
                distro=distro,
                image=image,
                branch=branch,
                host_mode=host_mode,
                container_mode=container_mode,
                skip_sync=skip_sync,
                dry_run=dry_run,
                keep_going=keep_going,
                clean=clean,
                show_layers=show_layers,
                sstate_mirror=sstate_mirror,
                sccache_dist=sccache_dist,
                sccache_scheduler=sccache_scheduler,
                target=target,
            )
            elapsed = time.monotonic() - t0
            status = "[green]passed[/]" if rc == 0 else "[red]failed[/]"
            results.append((release_id, status, elapsed))

        # Print summary table.
        table = Table(title="Multi-release build summary")
        table.add_column("Release", style="bold")
        table.add_column("Status")
        table.add_column("Duration")
        for release_id, status, elapsed in results:
            mins, secs = divmod(int(elapsed), 60)
            duration_str = f"{mins}m {secs:02d}s" if mins else f"{secs}s"
            table.add_row(release_id, status, duration_str)
        console.print(table)

        failures = sum(1 for _, status, _ in results if "failed" in status)
        if failures:
            console.print(f"[red]{failures} of {len(results)} release(s) failed.[/]")
            raise typer.Exit(code=1)
        raise typer.Exit(code=0)

    byo_form = kas_yaml is not None
    if byo_form and manifest is not None:
        console.print("[red]choose either a positional kas YAML or --manifest, not both[/]")
        raise typer.Exit(code=2)

    setup_dir = _bbsetup_workspace(workspace) if not byo_form and manifest is None else None
    if setup_dir is not None:
        _run_bbsetup_build(
            setup_dir,
            _BbsetupCtx(
                machine=machine,
                distro=distro,
                image=image,
                host_mode=host_mode,
                container_mode=container_mode,
                clean=clean,
                dry_run=dry_run,
                keep_going=keep_going,
                show_layers=show_layers,
                sstate_mirror=sstate_mirror,
                sccache_dist=sccache_dist,
                sccache_scheduler=sccache_scheduler,
                target=target,
                dry_run_script=dry_run_script,
            ),
        )
        return

    if not byo_form and manifest is None:
        pending = _uninitialized_bbsetup_dir(workspace)
        if pending is not None:
            console.print(
                f"[red]bitbake-setup workspace at {pending} is not initialized[/] "
                "- run `bitbake-setup init` first, then retry"
            )
            raise typer.Exit(code=2)

    main_yaml, user_extras = split_kas_yaml_arg(kas_yaml if byo_form else None)

    if byo_form:
        family, bsp = _dispatch_from_yaml(main_yaml)
    else:
        family, bsp = _dispatch_bsp(manifest)

    # BYO kas YAMLs carry the real MACHINE; without an explicit --machine the
    # family default ("generic") would otherwise land the artifacts path on a
    # nonexistent deploy/images/generic dir.
    if byo_form and machine is None:
        machine = machine_from_yaml(main_yaml)

    ws = _resolve_workspace(workspace, kas_yaml=main_yaml, family=family)

    # For preset builds, route all output into a composed subdirectory so
    # different presets and releases coexist without colliding in the same
    # workspace.  The override is workspace/build/<composed-path>; this lands
    # inside the existing build/ hierarchy so non-preset runs are unaffected.
    if active_preset is not None:
        ws = ws / "build" / compose_preset_output_path(active_preset, 0)

    cfg = resolve(
        workspace=ws,
        bsp_family=family,
        spec=BSPSpec(
            machine=machine,
            distro=distro,
            image=image,
            manifest=manifest,
            repo_branch=branch,
            host_mode=host_mode,
            container_mode=container_mode,
        ),
        kas_yaml=main_yaml,
        user_config=_state._USER_CONFIG,
        preset=active_preset,
    )
    if sstate_mirror is not None:
        cfg = replace(cfg, sstate_mirror_url=sstate_mirror)
    if sccache_dist:
        cfg = replace(cfg, sccache_dist=True)
    if sccache_scheduler is not None:
        cfg = replace(cfg, sccache_scheduler_url=sccache_scheduler)
    cfg = apply_mold_overrides(cfg)
    cfg = apply_scope_override(cfg)

    extra_overlays = _combine_overlays_with_tuning(user_extras, cfg)

    overlay_source = _overlay_for(bsp)
    if "KAS_CONTAINER_IMAGE" not in os.environ and cfg.kas_container_image != DEFAULT_CONTAINER_IMAGE:
        console.print(f"[dim]container image from config: {cfg.kas_container_image}[/]")

    effective_show_layers = show_layers or (_state._USER_CONFIG is not None and _state._USER_CONFIG.show_hashes)

    label = f"BYO {kas_yaml}" if byo_form else f"{cfg.machine} / {cfg.distro} / {cfg.image}"
    console.print(f"[bold]::[/] bakar build [{family}] {label}")

    if dry_run_script is not None:
        try:
            script = step_kas.generate_dry_run_script(
                cfg, cfg.kas_yaml, overlay_source, extra_overlays, keep_going=keep_going, target=target
            )
        except ValueError as exc:
            console.print(f"[red]Cannot generate dry-run script:[/] {exc}")
            raise typer.Exit(code=2) from None
        if dry_run_script == "-":
            sys.stdout.write(script)
        else:
            Path(dry_run_script).write_text(script)
        raise typer.Exit(code=0)

    if clean:
        _clean_build_dir(cfg)

    ctx = _BuildCtx(
        overlay_source=overlay_source,
        extra_overlays=extra_overlays,
        bsp=bsp,
        family=family,
        effective_show_layers=effective_show_layers,
        dry_run=dry_run,
        keep_going=keep_going,
        skip_sync=skip_sync,
        target=target,
    )

    cfg.runs_dir.mkdir(parents=True, exist_ok=True)
    with _open_run_logger(cfg) as log:
        overlays = [p for p in (cfg.kas_yaml, overlay_source, *extra_overlays) if p is not None]
        log.info(
            f"build mode={'byo' if byo_form else 'manifest'} bsp={family}, merging {len(overlays)} overlays:\n"
            + step_kas.friendly_overlay_lines(overlays, cfg.workspace),
        )
        if byo_form:
            _run_byo_build(cfg, log, ctx)
        else:
            _run_manifest_build(cfg, log, ctx)
