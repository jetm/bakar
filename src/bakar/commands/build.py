"""bakar build subcommand - full BSP build pipeline."""

from __future__ import annotations

import os
import sys
import time
from dataclasses import replace
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

import bakar.commands._app as _state
from bakar import feed as feed_mod
from bakar.bsp_detect import machine_from_yaml
from bakar.commands._app import app, console

# Re-exported, not merely imported. ``build()`` below calls every one of these
# as a bare name, and tests reach them as attributes of THIS module
# (``build_mod._run_single_preset_release`` is patched to count dispatches,
# ``build_mod._preset_completer`` and ``build_mod._BuildCtx`` are called
# directly), so the original import path has to keep resolving after the move.
from bakar.commands._build_flavors import (
    _BbsetupCtx,
    _BuildCtx,
    _is_multi_release,
    _preset_completer,
    _run_bbsetup_build,
    _run_byo_build,
    _run_manifest_build,
    _run_single_preset_release,
)
from bakar.commands._build_options import (
    BranchOption,
    CleanOption,
    CveOption,
    DistroOption,
    DryRunOption,
    DryRunScriptOption,
    FeedChannelOption,
    FeedOption,
    FeedReleaseOption,
    ImageOption,
    KasYamlArgument,
    KeepGoingOption,
    MachineOption,
    ManifestOption,
    OnOption,
    SbomOption,
    ShowLayersOption,
    SkipSyncOption,
    SstateMirrorOption,
    TargetOption,
    YesOption,
)
from bakar.commands._helpers import (
    WorkspaceOption,
    _bbsetup_workspace,
    _clean_build_dir,
    _combine_overlays_with_tuning,
    _dispatch_bsp,
    _dispatch_from_yaml,
    _overlay_for,
    _print_sstate_summary,
    _resolve_workspace,
    _uninitialized_bbsetup_dir,
    _workspace_from_cwd,
    apply_mold_overrides,
    apply_scope_override,
    global_container_mode,
    global_host_mode,
    global_output_mode_override,
    global_sccache_dist_override,
    split_kas_yaml_arg,
)

# Re-exported, not merely imported. Tests reach these as attributes of this
# module (build_mod._CveRequest, build_mod._SbomRequest, build_mod._FeedRequest)
# and _finish_build below reads several as bare names, so every name the
# post-build steps define stays resolvable here. A name meeting neither
# criterion does not belong: _CVE_REPORT_TARGET was re-exported and pinned in
# the same commit, so its pin was the only thing justifying it.
from bakar.commands._post_build import (
    _CveRequest,
    _FeedRequest,
    _filter_image_sbom,
    _generate_cve_report,
    _resolve_cve_request,
    _resolve_feed_request,
    _resolve_sbom_request,
    _SbomRequest,
    _sync_feed,
)
from bakar.config import DEFAULT_CONTAINER_IMAGE, BSPSpec, compose_preset_output_path, resolve
from bakar.fmt import fmt_duration
from bakar.observability import RunLogger
from bakar.output_mode import OutputMode, resolve_output_mode
from bakar.preset_config import load_presets

# step_override and step_qcom_build are read only by the flavor dispatchers in
# _build_flavors now. They stay because tests/test_cli_build_extended.py,
# tests/test_build_manifest_show_layers.py, tests/test_qcom_build.py and
# tests/test_cli_user_config.py patch `bakar.commands.build.step_override.apply`
# and `.step_qcom_build.run` - ATTRIBUTE patches, which mutate the shared step
# module and are therefore seen by the dispatchers wherever they live.
#
# The NAME form no longer binds. Patching `bakar.commands.build.step_override`
# itself rebinds a global nothing reads: _run_manifest_build lives in
# _build_flavors and resolves the name from THAT module. It used to bind, when
# the dispatcher was defined here. The patch still resolves, so the failure is
# silent - the real step runs. test_module_boundaries pins the object identity
# these attribute patches depend on.
from bakar.steps import bitbake_override as step_override  # noqa: F401
from bakar.steps import kas_build as step_kas
from bakar.steps import qcom_build as step_qcom_build  # noqa: F401
from bakar.steps.kas_build import KasBuildContext


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


def _finish_build(
    cfg,
    log,
    rc: int,
    machine: str,
    feed: _FeedRequest | None = None,
    cve: _CveRequest | None = None,
    sbom: _SbomRequest | None = None,
) -> None:
    """Shared build tail: rc check + triage hint, sstate summary, success line, artifacts path.

    ``machine`` names the deploy/images subdir - ``cfg.machine`` for byo/manifest
    builds, the bbsetup-translated machine for the bbsetup path.

    ``feed`` drives a sync then an index once the build has succeeded. The work
    sits here rather than in the callers because this function already returns
    early on a non-zero rc: a failed build cannot reach it, so "success only"
    holds for every call site including ones added later, rather than depending
    on each remembering to guard. A partial deploy staged into the feed would
    render a repository the index then advertises as installable.

    ``cve`` is the context the build just ran with, present only when ``--cve``
    was passed. The report is produced by a SECOND bitbake invocation derived
    from it, for the same reason it sits here rather than in the image graph:
    ``avocado-cve-report`` joins cve-check results across the whole tree, so a
    run scheduled alongside the ``do_cve_check`` tasks it reads would summarise a
    scan still in progress.

    A dry run is NOT such a success and the callers filter it out before this
    point, because ``run_build`` returns 0 after printing its preview - so rc
    alone cannot tell "built" from "never ran".
    """
    if rc != 0:
        exe = "kas" if cfg.host_mode else "kas-container"
        console.print(f"[red]{exe} build failed (exit {rc}).[/] Run `bakar triage {log.run_id}` for details.")
        raise typer.Exit(code=rc)
    # resolved_tmpdir is the single source of truth for the build TMPDIR: it
    # honors a local_tmpdir_base override (so the banner points at the real
    # images) and already encodes the family tmp-dir layout (tmp-glibc for qcom,
    # tmp elsewhere).
    deploy = cfg.resolved_tmpdir / "deploy" / "images" / machine
    if _state._USER_CONFIG is not None and _state._USER_CONFIG.show_sstate_summary:
        _print_sstate_summary(log.run_dir / "kas.log")
    console.print(f"[bold green]build succeeded[/] in {fmt_duration(time.monotonic() - log.start_monotonic)}")
    console.print(f"artifacts: {deploy}")

    # Before the feed sync: this reads the build tree and writes one JSON beside
    # the images, while the sync publishes that tree outward. Keeping every
    # build-tree step ahead of the publish keeps the order readable, and the two
    # do not otherwise interact - the report recipe inherits ``nopackages`` and
    # deletes its install and sysroot tasks, so it adds no RPM the feed could
    # pick up.
    if cve is not None:
        _generate_cve_report(cfg, cve, machine)

    # Also before the feed sync, and for a sharper reason than the CVE report's:
    # this produces the document a later publish step puts INTO the feed, so it
    # has to exist and be checked before anything publishes.
    filtered_sboms = _filter_image_sbom(cfg, sbom) if sbom is not None else []

    if feed is not None:
        # Only what THIS invocation filtered and checked reaches the feed. Reading
        # the output directory instead would publish whatever a previous run left
        # there, including a document produced before the filter was updated.
        _sync_feed(cfg, feed, sboms=filtered_sboms)


@app.command()
def build(
    kas_yaml: KasYamlArgument = None,
    machine: MachineOption = None,
    distro: DistroOption = None,
    image: ImageOption = None,
    target: TargetOption = None,
    manifest: ManifestOption = None,
    branch: BranchOption = None,
    skip_sync: SkipSyncOption = False,
    dry_run: DryRunOption = False,
    keep_going: KeepGoingOption = False,
    clean: CleanOption = False,
    workspace: WorkspaceOption = None,
    show_layers: ShowLayersOption = False,
    sstate_mirror: SstateMirrorOption = None,
    dry_run_script: DryRunScriptOption = None,
    # Inline rather than aliased: the completer lives in ``_build_flavors``,
    # which back-imports this module, so reaching it from ``_build_options``
    # would close a cycle that breaks when ``_build_options`` is imported
    # first. See that module's docstring.
    preset: Annotated[
        str | None,
        typer.Option(
            "--preset",
            autocompletion=_preset_completer,
            help="Named preset from config.toml; additive with explicit flags (explicit flags win).",
        ),
    ] = None,
    on: OnOption = None,
    yes: YesOption = False,
    feed: FeedOption = False,
    feed_release: FeedReleaseOption = feed_mod.DEFAULT_RELEASE,
    feed_channel: FeedChannelOption = feed_mod.DEFAULT_CHANNEL,
    cve: CveOption = False,
    sbom: SbomOption = False,
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

    # --feed writes into THIS host's feed, and --on moves the build to another
    # one. Allowing the combination renders the feed on the remote, where the
    # local `bakar feed serve` cannot see it - and where the next --on dispatch's
    # `rsync -a --delete` deletes it, because the feed roots are not in
    # RSYNC_EXCLUDES. Refusing beats silently building something that a later,
    # unrelated command destroys.
    if feed and on is not None:
        console.print(
            "[red]--feed cannot be combined with --on[/]: the feed would be rendered on the remote host. "
            "Build with --on, then run `bakar feed sync` there, or build locally."
        )
        raise typer.Exit(code=2)

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
        # Both paths are sent to the remote - ws_root as the rsync destination,
        # invoking_cwd as the directory the remote build runs from - so each has
        # to name a directory that exists THERE. Resolving them (which getcwd
        # and detect_kas_workspace both do) yields paths that only exist on the
        # node whose layout produced them: a shared workspace mounted at a
        # home-relative path on every node is a symlink onto the storage volume
        # on the node that owns it. See logical_path.
        from bakar.commands._helpers import logical_path
        from bakar.steps.remote_dispatch import dispatch_remote_build

        rc = dispatch_remote_build(
            on,
            logical_path(ws_root),
            logical_path(invoking_cwd),
            sys.argv[1:],
            sccache_dist=sccache_dist,
            assume_yes=yes,
        )
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

    # --feed validation, deliberately AFTER preset resolution: a bbsetup/generic
    # preset assigns kas_yaml just above, so testing the positional argument any
    # earlier refuses `--preset X --feed` for a preset that names a YAML - telling
    # the user their invocation is malformed when it is merely early.
    if feed:
        if kas_yaml is None:
            console.print("[red]--feed needs a kas YAML[/]: it names the build whose RPMs are staged.")
            raise typer.Exit(code=2)
        if active_preset is not None and _is_multi_release(active_preset):
            # One sync stages one deploy tree, and a multi-release fan-out
            # produces several. Refusing beats the alternative that shipped
            # first, where the flag was silently dropped and the build reported
            # success over an untouched feed.
            console.print(
                "[red]--feed cannot be combined with a multi-release preset[/]: each release would need its own "
                "sync. Build the releases, then run `bakar feed sync` per release."
            )
            raise typer.Exit(code=2)

    # Same placement argument as --feed above, and the same byo-only reach: the
    # report run is derived from the kas context, which the qcom path never
    # builds and the multi-release fan-out builds once per release. Refusing
    # beats reporting success over a report that was never produced.
    if cve:
        if kas_yaml is None:
            console.print("[red]--cve needs a kas YAML[/]: it names the build the report describes.")
            raise typer.Exit(code=2)
        if active_preset is not None and _is_multi_release(active_preset):
            console.print(
                "[red]--cve cannot be combined with a multi-release preset[/]: each release produces its own "
                "report. Build the releases, then run `bakar build <yaml> -t avocado-cve-report` per release."
            )
            raise typer.Exit(code=2)
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
        sccache_dist_override=global_sccache_dist_override(),
    )
    if sstate_mirror is not None:
        cfg = replace(cfg, sstate_mirror_url=sstate_mirror)
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
        feed=_resolve_feed_request(cfg, feed=feed, dry_run=dry_run, release=feed_release, channel=feed_channel),
        cve=_resolve_cve_request(cve=cve, dry_run=dry_run),
        sbom=_resolve_sbom_request(cfg, sbom=sbom, dry_run=dry_run),
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
