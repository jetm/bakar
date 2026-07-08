"""bakar report subcommand - success-path summary of a completed build run."""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Annotated, Literal

import typer

import bakar.commands._app as _state
from bakar.commands._app import app, console
from bakar.commands._helpers import (
    WorkspaceOption,
    _bbsetup_workspace,
    _find_run,
    _print_layer_hashes,
    _render_sstate_lines,
    _workspace_from_cwd,
)
from bakar.config import BSPSpec, resolve
from bakar.report import assemble_report


@app.command("report")
def report(
    run_id: Annotated[
        str | None,
        typer.Argument(help="Run ID (YYYYMMDD-HHMMSS). Latest if omitted."),
    ] = None,
    manifest: Annotated[
        str | None,
        typer.Option("--manifest", "-f", help="Manifest filename used to dispatch BSP family"),
    ] = None,
    workspace: WorkspaceOption = None,
    json_out: Annotated[
        bool,
        typer.Option("--json", help="Emit the summary as a single JSON object on stdout."),
    ] = False,
    show_sstate: Annotated[
        bool,
        typer.Option("--show-sstate", help="Show the sstate summary section."),
    ] = False,
) -> None:
    """Summarize a completed build run from its structured logs.

    Reads the resolved run's ``events.jsonl`` and layer git state and prints
    the run id, build status, duration, deploy directory and image size, and
    per-layer SHAs. With ``--json`` the same fields are emitted as one JSON
    object. Kernel version and recipe count are best-effort and omitted when
    unavailable.
    """
    family: Literal["nxp", "ti", "generic", "bbsetup"]
    if (setup_dir := _bbsetup_workspace(workspace)) is not None:
        runs_dirs: list[tuple[Path, Literal["nxp", "ti", "generic"]]] = [
            (setup_dir / "build" / "runs", "generic"),
        ]
        not_found_label = f"{runs_dirs[0][0]}"
        ws_for_cfg = setup_dir
        family = "bbsetup"
    else:
        ws = workspace or _workspace_from_cwd()
        runs_dirs = [
            (ws / "nxp" / "build" / "runs", "nxp"),
            (ws / "ti" / "build" / "runs", "ti"),
            (ws / "build" / "runs", "generic"),
        ]
        # Scan for meta-avocado / custom build-dir runs at ws/build-*/build/runs.
        for build_dir in sorted(ws.glob("build-*")):
            if build_dir.is_dir():
                extra = build_dir / "build" / "runs"
                runs_dirs.append((extra, "generic"))
        not_found_label = "nxp/build/runs/, ti/build/runs/, or build/runs/"
        ws_for_cfg = ws
        family = "nxp"  # provisional default; overwritten by the resolved run's label below

    found = _find_run(runs_dirs, run_id)
    if found is None:
        if run_id:
            console.print(f"[red]Run {run_id} not found under {not_found_label}[/]")
        else:
            console.print(f"[yellow]No runs found under {not_found_label}.[/]")
        raise typer.Exit(code=1)

    run_dir, label = found
    if family != "bbsetup":
        family = label
    # run_dir.parents[2] is always the correct bsp_root: ws/<fam>/build/runs/ID
    # -> ws/<fam> for nxp/ti, ws/build-<stem>/build/runs/ID -> ws/build-<stem>
    # for meta-avocado, and ws/build/runs/ID -> ws for plain generic.
    bsp_root_from_run = run_dir.parents[2]
    if family == "generic":
        # Resolve with workspace=bsp_root_from_run and bsp_family="bbsetup" so that
        # BuildConfig.bsp_root = workspace = bsp_root_from_run. Using kas_yaml_override
        # as a sentinel triggers is_meta_avocado when the workspace path contains
        # "meta-avocado" as a component, computing the wrong bsp_root.
        cfg = resolve(
            workspace=bsp_root_from_run,
            bsp_family="bbsetup",
            spec=BSPSpec(manifest=manifest),
            user_config=_state._USER_CONFIG,
        )
    else:
        cfg = resolve(
            workspace=ws_for_cfg,
            bsp_family=family,
            spec=BSPSpec(manifest=manifest),
            user_config=_state._USER_CONFIG,
        )

    summary = assemble_report(run_dir, cfg)

    effective_show_sstate = show_sstate or (_state._USER_CONFIG is not None and _state._USER_CONFIG.show_sstate_summary)

    # Presence of the buildhistory dir is the gate - no flag. ``assemble_report``
    # already parsed the tree and recorded whether it exists, so the section and
    # its JSON fields appear only when the user opted into buildhistory.
    has_buildhistory = summary.has_buildhistory

    # ccache builds persist raw ccache tool counters to ccache-stats.json (the
    # sccache stats live in the eventlog-derived cache_by_language section
    # instead). Read whichever artifact is present; a missing file omits its
    # section without error. Best-effort: a decode failure yields no section.
    ccache_stats: dict | None = None
    ccache_path = run_dir / "ccache-stats.json"
    try:
        if ccache_path.is_file():
            ccache_stats = json.loads(ccache_path.read_text())
    except OSError, ValueError:
        ccache_stats = None

    if json_out:
        payload = {
            "run_id": summary.run_id,
            "status": summary.status,
            "duration_s": summary.duration_s,
            "deploy_dir": summary.deploy_dir,
            "image_size": summary.image_size,
            "layers": [dataclasses.asdict(layer) for layer in summary.layers],
            "build_revision": summary.build_revision,
            "cache_by_language": {lang: dataclasses.asdict(stat) for lang, stat in summary.cache_by_language.items()},
            "dist_by_node": summary.dist_by_node,
            "task_family_rollup": {
                family: dataclasses.asdict(stat) for family, stat in summary.task_family_rollup.items()
            },
            "go_compile_seconds": summary.go_compile_seconds,
        }
        if effective_show_sstate:
            payload.update(
                {
                    "sstate_wanted": summary.sstate_wanted,
                    "sstate_local": summary.sstate_local,
                    "sstate_mirrors": summary.sstate_mirrors,
                    "sstate_missed": summary.sstate_missed,
                    "sstate_current": summary.sstate_current,
                    "sstate_match_pct": summary.sstate_match_pct,
                    "sstate_complete_pct": summary.sstate_complete_pct,
                }
            )
        if has_buildhistory:
            payload.update(
                {
                    "buildhistory_imagesize_kib": summary.buildhistory_imagesize_kib,
                    "top_packages": [list(pkg) for pkg in summary.top_packages],
                    "pkg_count": summary.pkg_count,
                    "layers_dirty": summary.layers_dirty,
                }
            )
        if ccache_stats is not None:
            payload["ccache_cache"] = {
                "cache_hits": ccache_stats.get("cache_hits"),
                "cache_misses": ccache_stats.get("cache_misses"),
                "hit_rate": ccache_stats.get("hit_rate"),
                "window": ccache_stats.get("window"),
            }
        print(json.dumps(payload))
        return

    console.print(f"[bold]::[/] report {summary.run_id}")
    status_colour = "green" if summary.status == "success" else "red"
    console.print(f"status: [{status_colour}]{summary.status}[/]")
    if summary.duration_s is not None:
        console.print(f"duration: {summary.duration_s:.0f}s")
    if summary.deploy_dir:
        console.print(f"deploy: {summary.deploy_dir}")
    if summary.image_size is not None:
        console.print(f"image size: {summary.image_size} bytes")
    if summary.build_revision is not None:
        console.print(f"build_revision: {summary.build_revision}")
    _print_layer_hashes(cfg, hashes=summary.layers)
    if effective_show_sstate:
        _render_sstate_lines(
            console,
            wanted=summary.sstate_wanted,
            local=summary.sstate_local,
            mirrors=summary.sstate_mirrors,
            missed=summary.sstate_missed,
            current=summary.sstate_current,
            match_pct=summary.sstate_match_pct,
            complete_pct=summary.sstate_complete_pct,
            header_style="bold",
        )
    if has_buildhistory:
        console.print("[bold]buildhistory:[/]")
        if summary.buildhistory_imagesize_kib is not None:
            console.print(f"  image size: {summary.buildhistory_imagesize_kib} KiB")
        if summary.pkg_count is not None:
            console.print(f"  packages: {summary.pkg_count}")
        if summary.top_packages:
            console.print("  top packages:")
            for pkg, size in summary.top_packages:
                console.print(f"    {pkg}: {size} KiB")
        if summary.layers_dirty:
            console.print(f"  dirty layers: {', '.join(summary.layers_dirty)}")
    if summary.cache_by_language:
        console.print("[bold]cache by language:[/]")
        for lang, stat in summary.cache_by_language.items():
            console.print(f"  {lang}: {stat.hits} hits, {stat.misses} misses, {stat.hit_rate:.1f}% hit rate")
        if summary.dist_by_node:
            console.print("  distribution:")
            for node, count in summary.dist_by_node.items():
                console.print(f"    {node}: {count}")
    if ccache_stats is not None:
        # Raw ccache tool counters. Deliberately free of per-language claims:
        # the sccache cache_by_language section above is eventlog-derived and
        # the two can visibly disagree post-delta. Older artifacts predate the
        # "window" field, so fall back to the historical "this build" label.
        window_label = "lifetime" if ccache_stats.get("window") == "lifetime" else "this build"
        console.print(f"[bold]ccache ({window_label}):[/]")
        console.print(f"  hits: {ccache_stats.get('cache_hits', 0)}")
        console.print(f"  misses: {ccache_stats.get('cache_misses', 0)}")
        console.print(f"  hit rate: {ccache_stats.get('hit_rate', 0.0):.1f}%")
    if any(stat.count for stat in summary.task_family_rollup.values()):
        total_family_s = sum(stat.seconds for stat in summary.task_family_rollup.values())
        console.print("[bold]task families:[/]")
        for family, stat in summary.task_family_rollup.items():
            share = 100.0 * stat.seconds / total_family_s if total_family_s else 0.0
            console.print(f"  {family}: {stat.seconds:.0f}s, {stat.count} tasks, {share:.1f}%")
        console.print(f"  go compile: {summary.go_compile_seconds:.0f}s")
