"""bakar build subcommand - full BSP build pipeline."""

from __future__ import annotations

import os
import sys
import time
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer
from rich.markup import escape
from rich.table import Table

import bakar.commands._app as _state
from bakar import feed as feed_mod
from bakar.bsp_detect import is_meta_avocado_yaml, machine_from_yaml, meta_avocado_checkout, meta_avocado_worktree
from bakar.commands import _detach_cmd, _helpers, _hints
from bakar.commands._app import app, console

# Re-exported, not merely imported. ``build()`` below calls every one of these
# as a bare name, and tests reach them as attributes of THIS module
# (``build_mod._run_single_preset_release`` is patched to count dispatches,
# ``build_mod._preset_completer`` and ``build_mod._BuildCtx`` are called
# directly), so the original import path has to keep resolving after the move.
from bakar.commands._build_common import (  # noqa: F401
    _finish_build,
    _open_run_logger,
    _output_mode,
    _plain_render_console,
)
from bakar.commands._build_flavors import (
    _BbsetupCtx,
    _BuildCtx,
    _is_multi_release,
    _preset_completer,
    _ReleaseCtx,
    _run_bbsetup_build,
    _run_byo_build,
    _run_manifest_build,
    _run_single_preset_release,
)
from bakar.commands._build_options import (
    BranchOption,
    CleanOption,
    CveOption,
    DetachOption,
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
    NoCaptureGraphOption,
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
    _resolve_workspace,
    _uninitialized_bbsetup_dir,
    _workspace_from_cwd,
    global_sccache_dist_override,
    global_sccache_scheduler,
    resolve_global_config,
    split_kas_yaml_arg,
)

# Re-exported, not merely imported. Tests reach these as attributes of this
# module (build_mod._CveRequest, build_mod._SbomRequest, build_mod._FeedRequest)
# and _finish_build below reads several as bare names, so every name the
# post-build steps define stays resolvable here. A name meeting neither
# criterion does not belong: _CVE_REPORT_TARGET was re-exported and pinned in
# the same commit, so its pin was the only thing justifying it.
from bakar.commands._post_build import (
    _CveRequest,  # noqa: F401
    _FeedRequest,  # noqa: F401
    _filter_image_sbom,  # noqa: F401
    _generate_cve_report,  # noqa: F401
    _resolve_cve_request,
    _resolve_feed_request,
    _resolve_sbom_request,
    _SbomRequest,  # noqa: F401
    _sync_feed,  # noqa: F401
)
from bakar.config import DEFAULT_CONTAINER_IMAGE, BSPSpec, ResolveRequest, compose_preset_output_path
from bakar.preset_config import load_presets

# step_override and step_qcom_build are read only by the flavor dispatchers in
# _build_flavors now. They stay because several tests patch
# `bakar.commands.build.step_override.apply` and `.step_qcom_build.run` -
# ATTRIBUTE patches, which mutate the shared step module and are therefore seen
# by the dispatchers wherever they live.
#
# Of the files that patch this way, only tests/test_build_manifest_show_layers.py
# was measured to FAIL when the seam is broken; the sites in
# test_cli_build_extended.py, test_qcom_build.py and test_cli_user_config.py
# patch it but pass either way. Do not read the number of patch sites as the
# number of tests holding this - it is one.
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

if TYPE_CHECKING:
    from bakar.user_config import UserConfig


_NO_ROUTING_MSG = "--on for meta-avocado builds needs \\[workspaces] template in the bakar user config"


def _dispatch_user_config() -> UserConfig | None:
    """The user config the app callback loaded, or loaded now; None after printing why it is invalid."""
    from bakar.user_config import load_user_config

    if _state._USER_CONFIG is not None:
        return _state._USER_CONFIG
    try:
        return load_user_config()
    except ValueError as exc:
        console.print(f"[red]Invalid bakar config:[/] {exc}")
        return None


def _refuse_release_workspace_mirror(ws_root: Path, host: str) -> bool:
    """True, after printing why, when ``ws_root`` is a release workspace the mirror must not reach.

    The mirror copies the workspace root with ``.git`` included, which moves a
    shared clone's HEAD on the host and overwrites edits there. A release
    workspace keeps its meta-avocado at the top, so its builds go through the
    layer-delivery flow instead; NXP, TI, qcom and bbsetup workspaces keep theirs
    under a family subdirectory or have none, and stay on the mirror.
    """
    from bakar.release_workspace import matches_release_workspace

    user_config = _dispatch_user_config()
    if user_config is None:
        return True
    configured = matches_release_workspace(ws_root, user_config.workspaces_template, user_config.workspaces_releases)
    if not configured and not (ws_root / "meta-avocado").is_dir():
        return False
    # A root that merely holds a meta-avocado directory (a layer repository whose
    # layer sits at <repo>/meta-avocado) is refused too, since the mirror would
    # still overwrite that clone, but calling it a release workspace misleads.
    reason = (
        "it is a configured release workspace, which bakar builds through the layer-delivery flow"
        if configured
        else "it holds a meta-avocado repository whose clone on the host the mirror would overwrite"
    )
    console.print(
        f"[red]cannot mirror {ws_root} to {host}[/]: {reason}. Run `bakar build <path to a kas file inside a "
        f"meta-avocado checkout> --on {host}` instead.",
    )
    return True


def _same_path(a: Path, b: Path) -> bool:
    """True when ``a`` and ``b`` name the same directory, as spelled or with symlinks resolved."""
    spellings = {os.path.normpath(a.expanduser()), os.path.realpath(a.expanduser())}
    return bool(spellings & {os.path.normpath(b.expanduser()), os.path.realpath(b.expanduser())})


def _detach_hint_lines(handle: str, run_id: str | None, host: str) -> list[str]:
    """Hint builder injected into DispatchFlags: steps/ may not import commands/_hints."""
    return _hints.after_detach(handle, run_id, on=host)


def _dispatch_meta_avocado(
    host: str,
    *,
    kas_arg: str,
    main_yaml: Path,
    invoking_cwd: Path,
    workspace: Path | None,
    sccache_dist: bool,
    assume_yes: bool,
    detach: bool = False,
) -> int:
    """Route a meta-avocado ``--on`` build to its release workspace and dispatch it.

    ``workspace`` is an explicit ``-w``: the release decides where the build
    runs, so a ``-w`` naming another workspace is refused rather than ignored.
    Every refusal here returns before any ssh, rsync or git command runs.
    """
    from bakar import config as config_mod
    from bakar.commands._helpers import logical_path
    from bakar.release_workspace import ReleaseRoutingError, map_release
    from bakar.steps import avocado_dispatch, kas_pins
    from bakar.steps.remote_dispatch import DispatchFlags

    user_config = _dispatch_user_config()
    if user_config is None:
        return 2
    if not (user_config.workspaces_template or user_config.workspaces_releases):
        # Refused before the release probe: with nothing configured no release
        # can map, so the kas dump would only delay the same answer.
        console.print(f"[red]{_NO_ROUTING_MSG}[/]: it names the release workspace on {host}.")
        return 2
    checkout = meta_avocado_checkout(main_yaml)
    if checkout is None:
        console.print(f"[red]cannot dispatch {main_yaml}[/]: no meta-avocado checkout holds it.")
        return 2
    try:
        release = kas_pins.probe_release(main_yaml, checkout)
        routed = map_release(release, user_config.workspaces_template, user_config.workspaces_releases)
    except ReleaseRoutingError as exc:
        console.print(f"[red]Cannot route {main_yaml} to a release workspace:[/] {exc}")
        return 2
    if routed is None:
        console.print(
            f"[red]{_NO_ROUTING_MSG}[/] (or a \\[workspaces.releases] entry for {release!r}): "
            f"it names the release workspace on {host}."
        )
        return 2
    # Without --on the same command builds in -w, so silently building in the
    # routed workspace instead would put the result somewhere the user did not name.
    # The fix cannot be "pass -w <routed>" alone: -w must exist locally, and the
    # routed workspace may exist only on the host.
    if workspace is not None and not _same_path(workspace, routed):
        console.print(
            f"[red]-w {workspace} does not match the release workspace {routed}[/] that the {release!r} "
            f"build is routed to on {host}: --on builds in the workspace the release maps to on {host}. "
            f"Drop -w (or point it at {routed} if that path exists locally)."
        )
        return 2
    worktree_root = user_config.dispatch_worktree_root
    return avocado_dispatch.dispatch_avocado_build(
        host,
        avocado_dispatch.AvocadoDispatchRequest(
            workspace=routed,
            checkout=logical_path(checkout),
            worktree=meta_avocado_worktree(main_yaml),
            kas_yaml=main_yaml,
            kas_arg=kas_arg,
            local_args=sys.argv[1:],
            release=release,
            worktree_root=Path(worktree_root) if worktree_root else None,
            build_dir_template=config_mod.worktree_build_template(user_config),
            cap_mb=user_config.dispatch_snapshot_untracked_cap_mb,
            invoking_cwd=invoking_cwd,
        ),
        DispatchFlags(
            sccache_dist=sccache_dist,
            assume_yes=assume_yes,
            detach=detach,
            hint_lines=_detach_hint_lines,
        ),
    )


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
    detach: DetachOption = False,
    yes: YesOption = False,
    feed: FeedOption = False,
    feed_release: FeedReleaseOption = feed_mod.DEFAULT_RELEASE,
    feed_channel: FeedChannelOption = feed_mod.DEFAULT_CHANNEL,
    cve: CveOption = False,
    sbom: SbomOption = False,
    no_capture_graph: NoCaptureGraphOption = False,
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
    # --sccache-dist / --sccache-scheduler are global callback options; read them
    # into the local names the body threads through. --host / --container are
    # folded in by resolve_global_config.
    sccache_dist = bool(global_sccache_dist_override())
    sccache_scheduler = global_sccache_scheduler()

    # --feed writes into THIS host's feed, and --on moves the build to another
    # one. Allowing the combination renders the feed on the remote, where the
    # local `bakar feed serve` cannot see it and nothing brings it back. Refusing
    # beats silently building a feed on a host that is not serving it.
    if feed and on is not None:
        console.print(
            "[red]--feed cannot be combined with --on[/]: the feed would be rendered on the remote host. "
            "Build with --on, then run `bakar feed sync` there, or build locally."
        )
        raise typer.Exit(code=2)

    # --clean, a clean KAS_TASK and a `recipe:clean*` target are forwarded with
    # everything else, and on the host each deletes a build directory or entries
    # of the shared download and sstate caches. Dispatch refuses every request
    # that asks for a deletion; what bitbake itself replaces inside the build's
    # own tmp tree is not such a request.
    if clean and on is not None:
        console.print(
            "[red]--clean cannot be combined with --on[/]: it would delete the build directory on the remote host. "
            "Build with --on, or run `bakar build --clean` on that host."
        )
        raise typer.Exit(code=2)
    if on is not None:
        from bakar.steps.remote_dispatch import deleting_requests

        requests = deleting_requests(target, os.environ, image=image)
        for request in requests:
            console.print(
                f"[red]{escape(request)} cannot be combined with --on[/]: it runs a clean task that would delete the "
                "target's build output, downloads or sstate on the remote host. Build with --on, or run the "
                "clean on that host.",
            )
        if requests:
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
        # Both paths are sent to the remote, so each has to name a directory that
        # exists THERE. Resolving them (which getcwd and detect_kas_workspace both
        # do) yields paths that only exist on the node whose layout produced
        # them: a shared workspace mounted at a home-relative path on every node
        # is a symlink onto the storage volume on the node that owns it. See
        # logical_path.
        from bakar.commands._helpers import logical_path

        # A meta-avocado build is routed to its release workspace BEFORE the local
        # workspace is resolved: only the remote copy of that workspace matters,
        # so one missing here must not block the dispatch.
        if kas_yaml is not None:
            _on_main_yaml, _ = split_kas_yaml_arg(kas_yaml)
            if is_meta_avocado_yaml(_on_main_yaml):
                rc = _dispatch_meta_avocado(
                    on,
                    kas_arg=kas_yaml,
                    main_yaml=_on_main_yaml,
                    invoking_cwd=invoking_cwd,
                    workspace=workspace,
                    sccache_dist=sccache_dist,
                    assume_yes=yes,
                    detach=detach,
                )
                raise typer.Exit(code=rc)
            # Mirror the workspace the local build would resolve, so a generic BYO
            # YAML run from outside a workspace does not exit 2, and a
            # manifest/bbsetup run mirrors the same tree the local build would.
            _on_family, _ = _dispatch_from_yaml(_on_main_yaml)
            ws_root = _resolve_workspace(workspace, kas_yaml=_on_main_yaml, family=_on_family)
        elif manifest is not None:
            _on_family, _ = _dispatch_bsp(manifest)
            ws_root = _resolve_workspace(workspace, family=_on_family)
        else:
            # A --preset resolves after this branch, so it lands here too and is
            # caught by the release-workspace check below like a bare build.
            ws_root = _bbsetup_workspace(workspace) or _workspace_from_cwd()
        if _refuse_release_workspace_mirror(ws_root, on):
            raise typer.Exit(code=2)
        from bakar.steps.remote_dispatch import DispatchFlags, MirrorRequest, dispatch_remote_build

        rc = dispatch_remote_build(
            on,
            MirrorRequest(ws_root=logical_path(ws_root), cwd=logical_path(invoking_cwd), local_args=sys.argv[1:]),
            DispatchFlags(sccache_dist=sccache_dist, assume_yes=yes, detach=detach, hint_lines=_detach_hint_lines),
        )
        raise typer.Exit(code=rc)
    # --detach: hand the whole command to a transient unit. Nothing is resolved
    # here (no workspace, no config); the unit re-runs the command and does that.
    if detach:
        if dry_run or dry_run_script is not None:
            console.print(
                "[red]--detach cannot be combined with --dry-run/--dry-run-script[/]; run it in the foreground."
            )
            raise typer.Exit(code=2)
        _detach_cmd.detach_and_exit(sys.argv[1:], _helpers.invoking_cwd())
    if _hints.agent_mode() and not (dry_run or dry_run_script is not None):
        _hints.emit(_hints.foreground_nudge())
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
                ctx=_ReleaseCtx(
                    workspace_root=ws_root,
                    machine=machine,
                    distro=distro,
                    image=image,
                    branch=branch,
                    skip_sync=skip_sync,
                    dry_run=dry_run,
                    keep_going=keep_going,
                    clean=clean,
                    show_layers=show_layers,
                    sstate_mirror=sstate_mirror,
                    sccache_scheduler=sccache_scheduler,
                    target=target,
                    no_capture_graph=no_capture_graph,
                ),
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
                clean=clean,
                dry_run=dry_run,
                keep_going=keep_going,
                show_layers=show_layers,
                sstate_mirror=sstate_mirror,
                sccache_scheduler=sccache_scheduler,
                target=target,
                dry_run_script=dry_run_script,
                no_capture_graph=no_capture_graph,
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

    ws = _resolve_workspace(workspace, kas_yaml=main_yaml, family=family, strict_routing=True)

    # For preset builds, route all output into a composed subdirectory so
    # different presets and releases coexist without colliding in the same
    # workspace.  The override is workspace/build/<composed-path>; this lands
    # inside the existing build/ hierarchy so non-preset runs are unaffected.
    if active_preset is not None:
        ws = ws / "build" / compose_preset_output_path(active_preset, 0)

    cfg = resolve_global_config(
        ResolveRequest(
            workspace=ws,
            bsp_family=family,
            spec=BSPSpec(
                machine=machine,
                distro=distro,
                image=image,
                manifest=manifest,
                repo_branch=branch,
            ),
            kas_yaml=main_yaml,
            user_config=_state._USER_CONFIG,
            preset=active_preset,
        )
    )
    if sstate_mirror is not None:
        cfg = replace(cfg, sstate_mirror_url=sstate_mirror)
    if sccache_scheduler is not None:
        cfg = replace(cfg, sccache_scheduler_url=sccache_scheduler)
    if no_capture_graph:
        cfg = replace(cfg, capture_graph=False)

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
                cfg,
                step_kas.KasBuildArgs(
                    kas_yaml=cfg.kas_yaml,
                    overlay_source=overlay_source,
                    extra_overlays=extra_overlays,
                    keep_going=keep_going,
                    target=target,
                ),
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
