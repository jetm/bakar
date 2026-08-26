"""bakar build subcommand - full BSP build pipeline."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer
from rich.console import Console
from rich.table import Table

import bakar.commands._app as _state
from bakar import cve_report, feed_ops, feed_preflight, sbom_publish
from bakar import feed as feed_mod
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
    global_sccache_dist_override,
    split_kas_yaml_arg,
)
from bakar.config import DEFAULT_CONTAINER_IMAGE, BSPSpec, compose_preset_output_path, resolve
from bakar.diagnostics import Status
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
    if sbom is not None:
        _filter_image_sbom(cfg, sbom)

    if feed is not None:
        _sync_feed(cfg, feed)


# The recipe in meta-avocado-sbom. EXCLUDE_FROM_WORLD, so naming it explicitly
# is the only way to reach it.
_CVE_REPORT_TARGET = "avocado-cve-report"


@dataclass(frozen=True)
class _CveRequest:
    """What ``--cve`` needs to run: the build's kas context and its overlays.

    The overlays are carried separately because ``run_build`` layers them from
    its keyword argument and never reads ``KasBuildContext.extra_overlays``. A
    report run derived from the context alone would therefore build against the
    bare YAML - and ``kas/feature/cve-check.yml``, which is what puts
    ``meta-avocado-sbom`` in bblayers, is normally stacked with colon syntax and
    arrives as exactly one of these overlays. Dropping them makes
    ``avocado-cve-report`` an unknown target on the one invocation the flag is
    for.
    """

    kas_ctx: KasBuildContext
    extra_overlays: list[Path]


def _resolve_cve_request(*, cve: bool, dry_run: bool) -> bool:
    """Report whether ``--cve`` should run after this build.

    The dry-run filter lives here rather than at the rc check for the same
    reason ``--feed``'s does: ``run_build`` prints its preview and returns 0, so
    rc reads a dry run as a success. Producing a report then would summarise
    whatever a PREVIOUS build left in ``CVE_CHECK_DIR`` - a valid-looking
    document describing a package set this invocation never wrote, from a
    command documented to exit before invoking kas.

    Unlike ``--feed`` there is no prerequisite probe to run up front. The
    prerequisite is cve-check data, which does not exist until the build has
    run, and the cost of finding out late is bounded: the check is a ``glob``
    and the build's artifacts are already on disk either way. ``--feed``'s
    pre-build gate exists because a missing ``createrepo_c`` costs a whole
    build's wall clock to learn, which does not apply here.
    """
    if not cve:
        return False
    if dry_run:
        console.print("[yellow]--dry-run: skipping --cve[/] (no build ran, so there is nothing to report on).")
        return False
    return True


def _generate_cve_report(cfg, request: _CveRequest, machine: str) -> None:
    """Run ``avocado-cve-report`` against the finished build.

    Skips rather than fails when the build carries no cve-check results. That is
    the ordinary shape of a build without ``kas/feature/cve-check.yml`` stacked,
    and the recipe would answer it with a ``bb.fatal`` after a full kas startup -
    a minute spent learning what a ``glob`` already established.

    A failure in the run itself does not fail the build, matching ``_sync_feed``:
    the build succeeded and its artifacts are on disk, and turning a post-step
    failure into a non-zero exit discards that over something the user can
    repeat with the one command named in the message.
    """
    cve_dir = cve_report.cve_data_dir(cfg, machine)
    if not cve_report.has_cve_data(cve_dir):
        console.print(
            f"[yellow]--cve: no cve-check results in {cve_dir}[/], so there is nothing to report on. "
            "Stack `kas/feature/cve-check.yml` onto the build and run it again."
        )
        return

    # dry_run is forced off rather than inherited: the caller already filtered a
    # dry run out, so a True here could only be stale - and would print a preview
    # while this function reported a report as produced.
    rc = step_kas.run_build(
        replace(request.kas_ctx, target=_CVE_REPORT_TARGET, dry_run=False),
        extra_overlays=request.extra_overlays,
    )
    if rc != 0:
        console.print(
            f"[yellow]build succeeded but the CVE report was not produced[/] "
            f"({_CVE_REPORT_TARGET} exited {rc}). "
            f"Re-run `bakar build {cfg.kas_yaml} -t {_CVE_REPORT_TARGET}` to see why."
        )
        return

    console.print(f"CVE report: {cve_report.report_path(cfg, machine)}")


@dataclass(frozen=True)
class _SbomRequest:
    """What ``--sbom`` needs to run: the workspace holding meta-avocado-sbom."""

    workspace: Path


def _resolve_sbom_request(cfg, *, sbom: bool, dry_run: bool) -> _SbomRequest | None:
    """Return the SBOM request for this build, or None when ``--sbom`` must not run.

    The missing-prerequisite case EXITS rather than returning None, which is the
    one place this diverges from ``--cve``. A checkout without the filter can
    never produce a publishable document, and that is knowable in a stat now
    versus a whole build's wall clock at the end - the same argument ``--feed``
    makes about ``createrepo_c``.

    Skipping instead would be worse than either: the per-image document exists
    whether or not the filter does, it carries vulnerability data (measured: 868
    ``security_*`` nodes and 303 CVE identifiers), and a silent skip leaves the
    user believing an SBOM step ran.
    """
    if not sbom:
        return None
    if dry_run:
        console.print("[yellow]--dry-run: skipping --sbom[/] (no build ran, so there is no document to filter).")
        return None

    lib = sbom_publish.sbom_lib_dir(cfg.workspace)
    if not sbom_publish.has_filter(lib):
        console.print(
            f"[red]--sbom cannot run: no publication filter at {lib}[/]. The per-image SPDX document "
            "carries vulnerability data and must be filtered before it can be published, and this "
            "meta-avocado checkout does not carry the filter that does it."
        )
        console.print("Update meta-avocado, or drop --sbom to build without producing a publishable inventory.")
        raise typer.Exit(code=2)

    return _SbomRequest(workspace=cfg.workspace)


def _filter_image_sbom(cfg, request: _SbomRequest) -> None:
    """Filter the build's per-image SPDX into a publishable inventory.

    Does not fail the build on any outcome, matching ``_sync_feed`` and
    ``_generate_cve_report``: the build succeeded and its artifacts are on disk.

    The independent leak check after the filter runs is not redundant with the
    filter's own ``--check``. It answers a narrower question at the moment that
    matters - is THIS file safe to publish - and it fails closed on a document it
    cannot read, because an unparseable file is not a file with no CVEs in it.
    """
    images = sbom_publish.images_dir(cfg)
    documents = sbom_publish.find_image_sboms(images)
    if not documents:
        console.print(
            f"[yellow]--sbom: no per-image SBOM under {images}[/]. A distro build only emits one when "
            "the image recipe's do_build is reached; check that avocado-distro depends on it."
        )
        return

    out_dir = cfg.resolved_tmpdir / "deploy" / "avocado-sbom"
    cmd, env = sbom_publish.filter_command(sbom_publish.sbom_lib_dir(request.workspace), images, out_dir)
    try:
        result = subprocess.run(cmd, env=env, capture_output=True, text=True, check=False)
    except OSError as exc:
        console.print(f"[yellow]build succeeded but the SBOM was not filtered:[/] {exc}")
        return

    if result.returncode != 0:
        console.print(
            f"[yellow]build succeeded but the SBOM filter exited {result.returncode}[/]: "
            f"{(result.stderr or '').strip().splitlines()[-1] if (result.stderr or '').strip() else 'no output'}"
        )
        return

    filtered = sbom_publish.find_image_sboms(out_dir)
    leaks = [reason for document in filtered for reason in sbom_publish.vulnerability_leaks(document)]
    if leaks:
        console.print("[red]--sbom: the filtered document is not publishable.[/] It still carries:")
        for reason in leaks:
            console.print(f"  {reason}")
        console.print("Do NOT publish it. This is a filter defect or a document shape it did not anticipate.")
        return

    for document in filtered:
        console.print(f"SBOM (publishable): {document}")


@dataclass(frozen=True)
class _FeedRequest:
    """What ``--feed`` needs to run: the build's YAML and where to render it."""

    kas_yaml: Path
    release: str
    channel: str


def _resolve_feed_request(
    cfg,
    *,
    feed: bool,
    dry_run: bool,
    release: str,
    channel: str,
) -> _FeedRequest | None:
    """Return the feed request for this build, or None when --feed must not run.

    Two reasons it returns None with the flag set. A dry run is one: ``run_build``
    prints its preview and returns 0, so rc alone reads it as a success, and
    syncing would stage whatever a PREVIOUS build happened to leave in the deploy
    tree and repin every client onto a fresh snapshot of stale RPMs - from a
    command documented to exit before invoking kas.

    The other is a failed prerequisite. The checks run HERE, before the build,
    rather than where ``bakar feed sync`` runs them, because this path's whole
    economics differ: a missing ``createrepo_c`` discovered after a multi-hour
    build costs that build's wall clock to learn, while the same probe costs
    milliseconds now. A blocking result refuses the whole command rather than
    downgrading to a warning - the user asked for a feed, and finding out at the
    end that they cannot have one is the outcome being avoided.
    """
    if not feed:
        return None
    if dry_run:
        console.print("[yellow]--dry-run: skipping --feed[/] (no build ran, so there is nothing new to stage).")
        return None

    # release/channel are passed so the codename cross-check runs here too: a
    # --feed-release that disagrees with the build's DISTRO_CODENAME renders into
    # a channel no client resolves, and that is worth catching before the build
    # rather than after it.
    results = feed_ops.preflight_results(cfg, cfg.kas_yaml, release=release, channel=channel)
    if feed_preflight.blocking(results):
        console.print("[red]--feed cannot run: the feed prerequisites are not met.[/]")
        for result in results:
            if result.status is Status.PASS:
                continue
            console.print(f"FAIL {result.name}: {result.message}")
            if result.fix_hint:
                console.print(f"     -> {result.fix_hint}")
        console.print("Fix the above, or drop --feed to build without touching the feed.")
        raise typer.Exit(code=2)

    return _FeedRequest(kas_yaml=cfg.kas_yaml, release=release, channel=channel)


def _sync_feed(cfg, request: _FeedRequest) -> None:
    """Stage the finished build into the feed, then rewrite ``targets.json``.

    Failures here do not fail the build. The build itself succeeded and its
    artifacts are on disk; turning a feed problem into a non-zero build exit
    would discard hours of work over a step the user can repeat with
    ``bakar feed sync``. The reason is printed with that hint instead.

    The except clause is deliberately broad. The paragraph above states an
    absolute - no feed problem fails the build - and a tuple of the failures
    currently anticipated does not implement it: ``parse_repo_map`` reads the map
    with no encoding, so one non-UTF-8 byte raises ``UnicodeDecodeError`` (a
    ``ValueError``, not an ``OSError``) and a signature drift in the feed layer
    raises ``TypeError``. Either would escape a narrow tuple and reach the user
    as a traceback AFTER "build succeeded" has printed, which is the one outcome
    this path exists to prevent.
    """
    try:
        result = feed_ops.sync_then_index(
            cfg,
            request.kas_yaml,
            release=request.release,
            channel=request.channel,
        )
    except Exception as exc:  # noqa: BLE001 - see the docstring: the contract is absolute
        console.print(f"[yellow]build succeeded but the feed was not updated:[/] {feed_ops.describe_failure(exc)}")
        console.print(
            f"Re-run `bakar feed sync --release {request.release} --channel {request.channel} "
            f"{request.kas_yaml}` once the cause is fixed; the build output is untouched."
        )
        return
    console.print(f"feed: snapshot {result['snapshot']} pinned for {', '.join(result['machines']) or '(none)'}")
    if result["unstaged"]:
        console.print(f"declared but not built: {', '.join(result['unstaged'])}")


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

    kas_ctx = _make_kas_ctx(cfg, log, ctx.overlay_source, ctx)
    rc = step_kas.run_build(
        kas_ctx,
        extra_overlays=ctx.extra_overlays,
        show_layers=ctx.effective_show_layers and not ctx.dry_run,
    )
    _finish_build(
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
    feed: Annotated[
        bool,
        typer.Option(
            "--feed",
            help="On build success, stage the RPMs into the local package feed and rewrite its "
            "index. A failed build never syncs, and neither does --dry-run.",
        ),
    ] = False,
    feed_release: Annotated[
        str,
        typer.Option("--feed-release", help="Feed release directory for --feed (see `bakar feed sync`)."),
    ] = feed_mod.DEFAULT_RELEASE,
    feed_channel: Annotated[
        str,
        typer.Option("--feed-channel", help="Feed channel directory for --feed (see `bakar feed sync`)."),
    ] = feed_mod.DEFAULT_CHANNEL,
    cve: Annotated[
        bool,
        typer.Option(
            "--cve",
            help="On build success, run avocado-cve-report to correlate the runtime packages with "
            "unpatched CVEs. Needs kas/feature/cve-check.yml stacked onto the build; skips with a "
            "note when the build carries no cve-check results. Neither a failed build nor --dry-run "
            "produces a report.",
        ),
    ] = False,
    sbom: Annotated[
        bool,
        typer.Option(
            "--sbom",
            help="On build success, filter the per-image SPDX document into a publishable inventory "
            "under deploy/avocado-sbom. Needs kas/feature/sbom.yml stacked onto the build and a "
            "meta-avocado checkout carrying the publication filter; refuses up front when the filter "
            "is absent, because the unfiltered document carries vulnerability data.",
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
