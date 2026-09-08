"""Per-family build dispatchers for the ``bakar build`` command.

Split off ``commands/build.py`` so that module holds the Typer command and its
flag plumbing while the four pipelines it dispatches to - bbsetup, byo,
manifest, and the single-preset-release wrapper - live here with the two
frozen contexts they read.

``build.py`` and this module import each other: ``build()`` calls the
dispatchers below, and the dispatchers call the run-logger, kas-context, and
build-tail helpers that stay on ``build.py`` next to the console and RunLogger
the tests patch there. The import at the BOTTOM of this file is what makes that
cycle safe in both directions - see the comment beside it.
"""

from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING

import typer

import bakar.commands._app as _state
from bakar.bsp_detect import machine_from_yaml
from bakar.commands._app import console
from bakar.commands._helpers import (
    _clean_build_dir,
    _combine_overlays_with_tuning,
    _dispatch_bsp,
    _dispatch_from_yaml,
    _overlay_for,
    _print_layer_hashes,
    _run_doctor_gate,
    _tuning_extra_overlays,
    apply_mold_overrides,
    apply_scope_override,
    global_sccache_dist_override,
    split_kas_yaml_arg,
)
from bakar.commands._post_build import _CveRequest
from bakar.config import DEFAULT_CONTAINER_IMAGE, BSPSpec, compose_preset_output_path, resolve
from bakar.kas import translate_bbsetup_config, write_bbsetup_yaml
from bakar.preset_config import load_presets
from bakar.steps import bitbake_override as step_override
from bakar.steps import kas_build as step_kas
from bakar.steps import qcom_build as step_qcom_build
from bakar.workspace import detect

if TYPE_CHECKING:
    from bakar.bsp_model import BspModel
    from bakar.commands._post_build import _FeedRequest, _SbomRequest


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
        sccache_dist_override=global_sccache_dist_override(),
    )
    if ctx.sstate_mirror is not None:
        cfg = replace(cfg, sstate_mirror_url=ctx.sstate_mirror)
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
        # resolved_tmpdir follows a host-mode local_tmpdir_base override off to
        # node-local disk; a hardcoded bsp_root/build/tmp would delete the empty
        # workspace path and strand the redirected (~200G) tmp.
        tmp_dir = cfg.resolved_tmpdir
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
    with _build_mod._open_run_logger(cfg) as log:
        log.info(f"build mode=bbsetup bsp=bbsetup yaml={cfg.kas_yaml} overlay={overlay_source}")

        _run_doctor_gate(cfg, log, None)

        write_bbsetup_yaml(
            setup_dir,
            target=bb_target,
            machine_override=ctx.machine,
            distro_override=ctx.distro,
        )

        kas_ctx = _build_mod._make_kas_ctx(cfg, log, overlay_source, ctx)
        rc = step_kas.run_build(
            kas_ctx,
            extra_overlays=_tuning_extra_overlays(cfg),
            show_layers=effective_show_layers,
        )
        _build_mod._finish_build(cfg, log, rc, translated["machine"])


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
    # The resolved feed request when --feed was passed, else None. Carried as the
    # resolved object rather than a bool because the kas YAML it names is not
    # always the positional argument - a preset supplies one too - so the
    # resolution has to happen once, where both sources are in scope.
    feed: _FeedRequest | None = None
    # Whether --cve survived resolution. A bool rather than a resolved object,
    # unlike ``feed`` above: what the report run needs is the kas context, and
    # that does not exist until the build path has built one.
    cve: bool = False
    # The resolved --sbom request, or None. An object rather than a bool because
    # its prerequisite is checked before the build, so the check has already run
    # by the time this is assembled.
    sbom: _SbomRequest | None = None


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

    kas_ctx = _build_mod._make_kas_ctx(cfg, log, ctx.overlay_source, ctx)
    rc = step_kas.run_build(
        kas_ctx,
        extra_overlays=ctx.extra_overlays,
        show_layers=ctx.effective_show_layers and not ctx.dry_run,
    )
    _build_mod._finish_build(
        cfg,
        log,
        rc,
        cfg.machine,
        feed=ctx.feed,
        # The same overlays this build ran with, not the context's own field -
        # see _CveRequest.
        cve=_CveRequest(kas_ctx=kas_ctx, extra_overlays=ctx.extra_overlays) if ctx.cve else None,
        sbom=ctx.sbom,
    )


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

        kas_ctx = _build_mod._make_kas_ctx(cfg, log, ctx.overlay_source, ctx)
        rc = step_kas.run_build(
            kas_ctx,
            extra_overlays=_tuning_extra_overlays(cfg),
            show_layers=ctx.effective_show_layers and not ctx.dry_run,
        )
    _build_mod._finish_build(cfg, log, rc, cfg.machine)


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
        sccache_dist_override=global_sccache_dist_override(),
    )
    if sstate_mirror is not None:
        cfg = replace(cfg, sstate_mirror_url=sstate_mirror)
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
        with _build_mod._open_run_logger(cfg) as log:
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


# Bound as the MODULE, not as the three names, and bound at the bottom rather
# than the top. Both details are load-bearing for the cycle with ``build.py``.
# Importing the names would fail when ``build.py`` is imported first, because it
# reaches its own ``from ... _build_flavors import`` line before defining them;
# binding the module object defers every attribute read to call time. Placing it
# last is what covers the other order - when this module is imported first, its
# own definitions are all in place by the time ``build.py`` imports them back.
from bakar.commands import build as _build_mod  # noqa: E402
