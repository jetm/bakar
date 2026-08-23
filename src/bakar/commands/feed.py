"""bakar feed subcommand - stage, render, serve and prune the local package feed.

Mirrors ``bakar prserv``: verbs over a module, workspace resolved per verb via
``--workspace/-w`` or by walking up from CWD.

``sync`` takes the kas YAML rather than deriving the machine from the workspace,
because the deploy tree a sync stages belongs to one build and the YAML is what
names it. Every other verb addresses the feed itself, which the config already
locates, so the YAML stays optional there - but every verb PRINTS the feed root
it resolved. Without the YAML the workspace comes from a CWD walk, which can land
on a different root than the one a sync used, and a destructive verb planning
against a feed the operator did not mean is not a failure they would otherwise
see.

``gc`` previews by default and needs ``--confirm`` to remove anything. That
asymmetry is deliberate and matches ``feed_reclaim``: a retained snapshot costs
disk, a wrongly removed pool entry costs a rebuild.

Library failures are translated here rather than allowed to propagate. The feed
modules raise on a missing scripts checkout and let ``subprocess`` raise on a
failed render, both by design; ``cli.py`` catches neither, so an untranslated
one reaches the user as a traceback.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Annotated

import typer

import bakar.commands._app as _state
from bakar import feed as feed_mod
from bakar import feed_index, feed_preflight, feed_retention, feed_serve
from bakar.commands._app import app, console
from bakar.commands._helpers import WorkspaceOption, _dispatch_bsp, _dispatch_from_yaml, _resolve_workspace
from bakar.config import BuildConfig, resolve
from bakar.diagnostics import Status

feed_app = typer.Typer(
    help="Manage the local package feed (sync/index/serve/stop/status/gc).",
    no_args_is_help=True,
)

KasYamlArgument = Annotated[
    Path,
    typer.Argument(exists=True, help="kas YAML naming the build whose RPMs are staged"),
]

OptionalKasYaml = Annotated[
    Path | None,
    typer.Argument(exists=False, help="Optional kas YAML; routes through _dispatch_from_yaml"),
]

ReleaseOption = Annotated[str, typer.Option("--release", help="Feed release directory")]
ChannelOption = Annotated[str, typer.Option("--channel", help="Feed channel directory")]
PortOption = Annotated[int, typer.Option("--port", help="Port the static server binds")]
BindOption = Annotated[
    str,
    typer.Option("--bind", help="Interface to bind; the loopback default keeps the feed off the network"),
]


def _resolve_cfg(workspace: Path | None = None, kas_yaml: Path | None = None) -> BuildConfig:
    """Resolve the :class:`BuildConfig` for the current workspace (see bakar prserv)."""
    if kas_yaml is not None:
        family, _bsp = _dispatch_from_yaml(kas_yaml)
    else:
        family, _bsp = _dispatch_bsp(None)
    ws = _resolve_workspace(workspace, kas_yaml=kas_yaml, family=family)
    return resolve(
        workspace=ws,
        bsp_family=family,
        kas_yaml=kas_yaml,
        user_config=_state._USER_CONFIG,
    )


def _report_preflight(results: list) -> bool:
    """Print prerequisite results and return whether any of them blocks.

    Only prints the passing INFO lines and every failure - a wall of green ticks
    on the common path buries the one line that matters.
    """
    blockers = feed_preflight.blocking(results)
    failures = [r for r in results if r.status is Status.FAIL]

    for result in failures:
        console.print(f"[{result.severity}] {result.name}: {result.message}")
        if result.fix_hint:
            console.print(f"    -> {result.fix_hint}")

    if blockers:
        console.print(
            f"{len(blockers)} prerequisite(s) missing; nothing was staged. Run `bakar feed doctor` after fixing them."
        )
    return bool(blockers)


@feed_app.command("doctor")
def doctor(
    kas_yaml: OptionalKasYaml = None,
    workspace: WorkspaceOption = None,
) -> None:
    """Check everything a feed sync needs, without touching the feed.

    Runs with or without a kas YAML, and without a workspace at all: the host
    tools are the tier a first-time user needs first, and that question has an
    answer before any workspace exists. Refusing to answer it because workspace
    detection failed would make the first command run after `pip install bakar`
    report the wrong problem.
    """
    scripts: Path | None = None
    deploy: Path | None = None
    feed_root: Path | None = None
    stage_root: Path | None = None

    try:
        cfg = _resolve_cfg(workspace, kas_yaml)
    except typer.Exit, SystemExit:
        # _resolve_workspace prints its own diagnosis and raises typer.Exit(2).
        # click is deliberately not caught: typer no longer depends on it, so
        # naming it would be an undeclared import for an exception that cannot
        # reach this frame.
        console.print("continuing with host prerequisites only\n")
    else:
        feed_root = feed_mod.resolve_feed_root(cfg)
        stage_root = feed_mod.resolve_stage_root(cfg)
        if kas_yaml is not None:
            deploy = _deploy_dir(cfg)
            try:
                scripts = feed_mod.meta_avocado_scripts(kas_yaml)
            except FileNotFoundError:
                scripts = None

    results = feed_preflight.preflight(
        feed_root=feed_root,
        stage_root=stage_root,
        scripts=scripts,
        deploy_dir=deploy,
    )

    for result in results:
        mark = "ok  " if result.status is Status.PASS else "FAIL"
        console.print(f"{mark} {result.name}: {result.message}")
        if result.fix_hint:
            console.print(f"     -> {result.fix_hint}")

    if feed_preflight.blocking(results):
        raise typer.Exit(code=1)
    if feed_root is None:
        console.print(
            "host prerequisites met; run from a workspace (or pass --workspace) to also check "
            "the feed paths, and add a kas YAML for the layer checkout and build output"
        )
    elif kas_yaml is None:
        console.print("host prerequisites met; pass a kas YAML to also check the layer checkout and build output")


def _deploy_dir(cfg: BuildConfig) -> Path:
    """Return the RPM deploy directory the build wrote.

    ``avocado-repo.map`` sits here rather than one level up, and the map is what
    declares which repositories a sync renders - so this is the directory the
    feed stages from, not ``deploy`` itself.
    """
    return cfg.resolved_tmpdir / "deploy" / "rpm"


@feed_app.command("sync")
def sync(
    kas_yaml: KasYamlArgument,
    workspace: WorkspaceOption = None,
    release: ReleaseOption = feed_mod.DEFAULT_RELEASE,
    channel: ChannelOption = feed_mod.DEFAULT_CHANNEL,
) -> None:
    """Stage a finished build and render every repository it declares."""
    cfg = _resolve_cfg(workspace, kas_yaml)
    deploy = _deploy_dir(cfg)

    try:
        scripts = feed_mod.meta_avocado_scripts(kas_yaml)
    except FileNotFoundError:
        scripts = None

    # Every prerequisite at once, before anything is staged. The feed depends on
    # a native binary and two shell tools that pip cannot install, so a
    # first-time run on a fresh machine typically fails several checks - and
    # surfacing them one exception at a time costs a round trip each.
    results = feed_preflight.preflight(
        feed_root=feed_mod.resolve_feed_root(cfg),
        stage_root=feed_mod.resolve_stage_root(cfg),
        scripts=scripts,
        deploy_dir=deploy,
        release=release,
        channel=channel,
    )
    if _report_preflight(results) or scripts is None:
        raise typer.Exit(code=1)

    console.print(f"feed: {feed_mod.resolve_feed_root(cfg)}")
    try:
        result = feed_mod.sync(
            cfg,
            deploy_dir=deploy,
            scripts=scripts,
            release=release,
            channel=channel,
        )
    except subprocess.CalledProcessError as exc:
        # The staging and render scripts run with check=True. A non-zero exit
        # leaves the snapshot pointer unwritten by design, so the feed is intact;
        # what the user needs is which script failed, not a traceback.
        console.print(
            f"feed sync failed: {Path(exc.cmd[0]).name} exited {exc.returncode}. "
            "The snapshot pointer was not written, so the feed still serves the previous snapshot."
        )
        raise typer.Exit(code=1) from exc
    except FileNotFoundError as exc:
        console.print(f"feed sync failed: {exc}")
        raise typer.Exit(code=1) from exc

    console.print(f"snapshot: {result['snapshot']}")
    console.print(f"channel:  {result['channel_root']}")
    console.print(f"rendered: {', '.join(result['repos']) or '(none)'}")
    console.print(f"pinned:   {', '.join(result['machines']) or '(no machine repo rendered)'}")
    if result["unstaged"]:
        # Declared-but-absent is normal - a map lists what a machine could
        # publish - so this is reported rather than treated as a failure.
        console.print(f"declared but not built: {', '.join(result['unstaged'])}")


@feed_app.command("index")
def index(
    kas_yaml: OptionalKasYaml = None,
    workspace: WorkspaceOption = None,
    release: ReleaseOption = feed_mod.DEFAULT_RELEASE,
    channel: ChannelOption = feed_mod.DEFAULT_CHANNEL,
) -> None:
    """Write ``targets.json`` from what the channel has actually rendered."""
    cfg = _resolve_cfg(workspace, kas_yaml)
    feed_root = feed_mod.resolve_feed_root(cfg)
    channel_dir = feed_mod.channel_root(feed_root, release=release, channel=channel)
    if not channel_dir.is_dir():
        # Writing the index would mkdir the channel, so a typo in --channel would
        # otherwise create a new empty one and report "(none rendered yet)" as if
        # the channel were merely unbuilt.
        console.print(f"no such channel: {channel_dir}. Run `bakar feed sync` first, or check --release/--channel.")
        raise typer.Exit(code=1)

    path = feed_index.write_targets_index(channel_dir)
    machines = list(feed_index.derive_targets(channel_dir))
    console.print(f"feed: {feed_root}")
    console.print(f"wrote {path}")
    console.print(f"targets: {', '.join(machines) or '(none rendered yet)'}")


@feed_app.command("serve")
def serve(
    kas_yaml: OptionalKasYaml = None,
    workspace: WorkspaceOption = None,
    port: PortOption = feed_serve.DEFAULT_PORT,
    bind: BindOption = feed_serve.DEFAULT_BIND,
) -> None:
    """Serve the feed root over HTTP in the background."""
    cfg = _resolve_cfg(workspace, kas_yaml)
    root = feed_mod.resolve_feed_root(cfg)
    if feed_serve.is_serving(root):
        running_port = feed_serve.recorded_port(root)
        where = f" at http://localhost:{running_port}" if running_port is not None else ""
        console.print(f"already serving {root}{where}")
        return

    pid = feed_serve.start_serving(root, port=port, bind=bind)
    if pid is None:
        console.print(
            f"failed to serve {root}: nothing came up on {bind}:{port} - the port is "
            "most likely already in use. Pass --port to pick another."
        )
        raise typer.Exit(code=1)
    console.print(f"serving {root} at http://localhost:{port} (bind {bind}, pid {pid})")


@feed_app.command("stop")
def stop(
    kas_yaml: OptionalKasYaml = None,
    workspace: WorkspaceOption = None,
) -> None:
    """Stop the feed's static server."""
    cfg = _resolve_cfg(workspace, kas_yaml)
    if feed_serve.stop_serving(feed_mod.resolve_feed_root(cfg)):
        console.print("stopped")
    else:
        console.print("not running")


@feed_app.command("status")
def status(
    kas_yaml: OptionalKasYaml = None,
    workspace: WorkspaceOption = None,
    release: ReleaseOption = feed_mod.DEFAULT_RELEASE,
    channel: ChannelOption = feed_mod.DEFAULT_CHANNEL,
    port: PortOption = feed_serve.DEFAULT_PORT,
) -> None:
    """Report what the feed holds, without starting anything."""
    cfg = _resolve_cfg(workspace, kas_yaml)
    report = feed_serve.feed_status(
        feed_mod.resolve_feed_root(cfg),
        release=release,
        channel=channel,
        port=port,
    )
    targets = report["targets"]
    console.print(f"feed:     {report['feed_root']}")
    console.print(f"channel:  {report['channel_root']}")
    console.print(f"targets:  {', '.join(targets) or '(none)'}")
    console.print(f"packages: {report['pool_entries']}")
    console.print(f"snapshot: {', '.join(report['snapshots']) or '(none)'}")
    console.print(f"serving:  {'yes, ' + str(report['url']) if report['serving'] else 'no'}")

    # The stage root is a fourth growth source and `gc` does not touch it, so
    # reporting its size here is what keeps "gc freed nothing" from reading as
    # "nothing is using disk".
    stage = feed_mod.resolve_stage_root(cfg)
    if stage.is_dir():
        console.print(f"stage:    {stage} ({_tree_gib(stage):.2f} GiB, not pruned by gc)")


def _tree_gib(root: Path) -> float:
    """Return a directory's size in GiB, ignoring what cannot be stat'd."""
    total = 0
    for entry in root.rglob("*"):
        if entry.is_file() and not entry.is_symlink():
            try:
                total += entry.stat().st_size
            except OSError:
                continue
    return total / 2**30


@feed_app.command("gc")
def gc(
    kas_yaml: OptionalKasYaml = None,
    workspace: WorkspaceOption = None,
    release: ReleaseOption = feed_mod.DEFAULT_RELEASE,
    channel: ChannelOption = feed_mod.DEFAULT_CHANNEL,
    keep: Annotated[int, typer.Option("--keep", min=1, help="Snapshots to retain by age")] = 3,
    confirm: Annotated[bool, typer.Option("--confirm", help="Actually remove; default previews")] = False,
) -> None:
    """Prune old snapshots, stale metadata and orphaned pool entries."""
    cfg = _resolve_cfg(workspace, kas_yaml)
    feed_root = feed_mod.resolve_feed_root(cfg)
    channel_dir = feed_mod.channel_root(feed_root, release=release, channel=channel)

    # Refuse rather than plan against a channel that is not there. Without a kas
    # YAML the workspace comes from a CWD walk, so the resolved feed root can be
    # one this operator never synced - and an absent channel is what that looks
    # like. Planning it would report a clean no-op for the wrong tree.
    if not channel_dir.is_dir():
        console.print(
            f"no such channel: {channel_dir}. Nothing was examined. Check --release/--channel, "
            "or pass the kas YAML (or --workspace) so the feed root resolves to the one you synced."
        )
        raise typer.Exit(code=1)

    console.print(f"feed:    {feed_root}")
    console.print(f"channel: {channel_dir}")

    plan = feed_retention.plan_retention(channel_dir, feed_root=feed_root, keep=keep)

    if plan.unreadable_pointers:
        console.print(
            f"{len(plan.unreadable_pointers)} pointer(s) exist but name no snapshot, so which "
            f"ones clients are pinning is unknown; no snapshot will be removed. "
            f"First: {plan.unreadable_pointers[0]}"
        )

    if plan.foreign_snapshot_entries:
        console.print(
            f"ignored under snapshots/: {', '.join(plan.foreign_snapshot_entries)} "
            "(not snapshot ids, or symlinks) - left alone, and not counted against --keep"
        )

    if plan.pool_reclaim_suppressed:
        console.print(
            f"pool reclamation SUPPRESSED: {len(plan.unreadable_primaries)} package list(s) "
            "could not be read, so which pool entries they reference is unknown. "
            f"First: {plan.unreadable_primaries[0]}"
        )

    if plan.is_empty:
        console.print(
            f"nothing to reclaim: {len(plan.kept_snapshots)} snapshot(s) retained, "
            "no stale metadata, no orphaned pool entries"
        )
        return

    result = feed_retention.apply_retention(plan, feed_root=feed_root, confirm=confirm)
    verb = "removed" if result.applied else "would remove"

    console.print(f"{verb} snapshots: {', '.join(result.removed_snapshots) or '(none)'}")
    console.print(f"{verb} stale metadata: {len(result.removed_metadata)} file(s)")
    console.print(f"{verb} pool entries: {len(result.removed_pool)}")
    console.print(f"{'freed' if result.applied else 'would free'}: {result.freed_bytes / 2**30:.2f} GiB")
    console.print(f"retained snapshots: {', '.join(plan.kept_snapshots) or '(none)'}")

    if plan.unreadable_indexes:
        console.print(
            f"skipped {len(plan.unreadable_indexes)} repository(ies) whose repomd.xml "
            "could not be read; their metadata was left alone"
        )

    if result.failed_snapshots:
        console.print(f"FAILED to remove: {', '.join(result.failed_snapshots)}")

    if not result.applied:
        console.print("preview only; pass --confirm to remove")
        return

    if not result.audit_clean:
        # A dangling reference means the run left a repository serving an index
        # that names a package which is gone - worse than a missing repository,
        # because it fails at download rather than at resolve. An unreadable
        # primary is not a pass either: it is a repository the audit could not
        # check, so reporting it as clean is how a real break hides.
        for idx, missing in result.dangling[:10]:
            console.print(f"  dangling: {idx} -> {missing}")
        for primary in result.unreadable_primaries[:10]:
            console.print(f"  unverifiable: {primary}")
        console.print(
            f"ERROR: post-run audit not clean - {len(result.dangling)} dangling reference(s), "
            f"{len(result.unreadable_primaries)} unreadable package list(s)"
        )
        raise typer.Exit(code=1)


app.add_typer(feed_app, name="feed")
