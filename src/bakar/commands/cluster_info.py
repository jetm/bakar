"""bakar cluster-info subcommand - live sccache-dist scheduler capacity."""

from __future__ import annotations

import json
from typing import Annotated

import typer

import bakar.commands._app as _state
from bakar.cache_render import daemon_doc
from bakar.commands._app import app, console
from bakar.diagnostics import probe_build_daemon, probe_cluster


@app.command("cluster-info")
def cluster_info(
    scheduler: Annotated[
        str | None,
        typer.Option("--scheduler", help="Scheduler URL override (default: from --sccache-scheduler or config)"),
    ] = None,
    output_json: Annotated[
        bool,
        typer.Option("--json", help="Emit a JSON document with the scheduler capacity"),
    ] = False,
) -> None:
    """Query the sccache-dist scheduler and print its live capacity.

    Reports the aggregate the scheduler exposes: build-server count, total CPU
    count, and jobs in progress. Per-node detail is not available from the
    upstream scheduler; when a forked scheduler exposes a per-server array it is
    printed as a node list without any further change here.

    Resolves the scheduler URL from --scheduler, then the global
    --sccache-scheduler, then the user config's sccache_scheduler_url. Exits 1
    when the scheduler is unreachable or sccache is not installed.
    """
    url = scheduler
    if url is None:
        url = _state._SCCACHE_SCHEDULER
    if url is None and _state._USER_CONFIG is not None:
        url = _state._USER_CONFIG.sccache_scheduler_url

    report = probe_cluster(url)
    cap = report.capacity
    daemon = probe_build_daemon()

    if output_json:
        doc = {
            "reachable": report.reachable,
            "scheduler_url": url,
            "error": report.error,
            "capacity": (
                {
                    "num_servers": cap.num_servers,
                    "num_cpus": cap.num_cpus,
                    "in_progress": cap.in_progress,
                    "servers": cap.servers,
                }
                if cap is not None
                else None
            ),
            "build_daemon": daemon_doc(daemon),
        }
        typer.echo(json.dumps(doc, indent=2))
        if not report.reachable:
            raise typer.Exit(1)
        return

    if not report.reachable or cap is None:
        console.print(f"[red]cluster unreachable:[/] {report.error}", highlight=False)
        raise typer.Exit(1)

    console.print("sccache-dist cluster:", highlight=False)
    console.print(f"  scheduler: {url or '(from sccache config)'}", highlight=False)
    console.print(f"  build servers: {cap.num_servers}", highlight=False)
    console.print(f"  cpus: {cap.num_cpus}", highlight=False)
    console.print(f"  jobs in progress: {cap.in_progress}", highlight=False)
    if cap.servers:
        console.print("  nodes:", highlight=False)
        for node in cap.servers:
            if isinstance(node, dict):
                node_id = node.get("id", "?")
                cpus = node.get("num_cpus", "?")
                jobs = node.get("in_progress", 0)
                console.print(f"    {node_id} - {cpus} cpus, {jobs} job(s)", highlight=False)
            else:
                console.print(f"    {node}", highlight=False)

    if daemon.running:
        console.print(f"  build daemon (container {daemon.container or '?'}):", highlight=False)
        if daemon.error:
            console.print(f"    [yellow]stats unavailable:[/] {daemon.error}", highlight=False)
        else:
            local = max(daemon.cache_misses - daemon.distributed, 0)
            console.print(f"    cache: {daemon.cache_hits} hits / {daemon.cache_misses} misses", highlight=False)
            for lang in sorted(set(daemon.cache_hits_by_lang) | set(daemon.cache_misses_by_lang)):
                lh = daemon.cache_hits_by_lang.get(lang, 0)
                lm = daemon.cache_misses_by_lang.get(lang, 0)
                lt = lh + lm
                lr = 100 * lh / lt if lt else 0.0
                console.print(f"      cache[{lang}]: {lh}/{lm} hit/miss ({lr:.0f}% hit)", highlight=False)
            console.print(
                f"    compiles: {daemon.distributed} distributed (all langs), {local} local, "
                f"{daemon.dist_errors} dist errors",
                highlight=False,
            )
            for addr, n in daemon.per_node:
                console.print(f"      dist[{addr}]: {n}", highlight=False)
            if daemon.cache_location:
                console.print(f"    cache location: {daemon.cache_location}", highlight=False)
            colour = {"DISTRIBUTING": "green", "LOCAL-ONLY": "red"}.get(daemon.verdict, "yellow")
            console.print(f"    [{colour}]verdict: {daemon.verdict}[/]", highlight=False)
