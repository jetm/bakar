"""bakar sched-triage subcommand - turn the R0 scheduler+client logs into a triage report.

Reads the scheduler journal (``dist-alloc``/``dist-status``) and the client
``SCCACHE_ERROR_LOG`` and reports the four cluster-utilisation signals in one
place instead of manual ``journalctl``/``grep``: scheduler misroute rate (W1),
cluster saturation, per-job timers (W2), local fallbacks + remote rustc errors
(W3). Read-only.
"""

from __future__ import annotations

import dataclasses
import json
import os
import subprocess
from pathlib import Path
from typing import Annotated

import typer

from bakar.commands._app import app, console
from bakar.sched_triage import (
    conditioned_util,
    parse_client_log,
    parse_dist_alloc,
    parse_dist_status,
    parse_dist_status_series,
    time_weighted_util,
)


def _compile_intervals(events_path: Path) -> list[tuple[float, float]]:
    """do_compile (started, completed) epoch spans from bitbake-events.json.

    Feeds the per-phase util join (conditioned_util). Any error - missing file,
    bad JSON, wrong shape - yields an empty list so the caller never guards it.
    """
    try:
        with events_path.open(encoding="utf-8") as fh:
            artifact = json.load(fh)
    except OSError, ValueError:
        return []
    rows = artifact.get("tasks") if isinstance(artifact, dict) else None
    intervals: list[tuple[float, float]] = []
    for row in rows or []:
        if not isinstance(row, dict) or row.get("task") != "do_compile":
            continue
        try:
            start, end = float(row["started"]), float(row["completed"])
        except KeyError, TypeError, ValueError:
            continue
        if end >= start:
            intervals.append((start, end))
    return intervals


def _journal_lines(unit: str, since: str) -> list[str]:
    """Return the unit's journal lines since ``since`` (empty on any failure)."""
    try:
        out = subprocess.run(
            # -o short-unix prefixes every line with an epoch so the poll series
            # can be joined to the bitbake task timeline (per-phase util).
            ["journalctl", "-u", unit, "--since", since, "--no-pager", "-o", "short-unix"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except OSError, subprocess.SubprocessError:
        return []
    return out.stdout.splitlines()


@app.command("sched-triage")
def sched_triage(
    since: Annotated[
        str,
        typer.Option("--since", help="journalctl --since window for the scheduler unit."),
    ] = "1 hour ago",
    client_log: Annotated[
        Path | None,
        typer.Option("--client-log", help="Client SCCACHE_ERROR_LOG path (default: $SCCACHE_ERROR_LOG)."),
    ] = None,
    unit: Annotated[
        str,
        typer.Option("--unit", help="Scheduler systemd unit."),
    ] = "sccache-scheduler.service",
    events: Annotated[
        Path | None,
        typer.Option("--events", help="bitbake-events.json for the per-phase (do_compile supply) util join."),
    ] = None,
    output_json: Annotated[
        bool,
        typer.Option("--json", help="Emit the triage report as one JSON object."),
    ] = False,
) -> None:
    """Aggregate the scheduler + client logs into a cluster-utilisation triage report.

    Parses ``dist-alloc``/``dist-status`` from the scheduler journal and the
    per-compile timers, fallbacks, and remote rustc errors from the client log.
    Requires the R0 scheduler drop-in (``SCCACHE_LOG=info``) and a client run
    logged to ``SCCACHE_ERROR_LOG``; sections whose source is empty report zero.
    """
    journal = _journal_lines(unit, since)
    alloc = parse_dist_alloc(journal)
    sat = parse_dist_status(journal)
    series = parse_dist_status_series(journal)
    weighted = time_weighted_util(series)

    log_path = client_log or (Path(os.environ["SCCACHE_ERROR_LOG"]) if os.environ.get("SCCACHE_ERROR_LOG") else None)
    # Stream the log line-by-line rather than read_text().splitlines(): the client
    # SCCACHE_ERROR_LOG can be hundreds of MB, and parse_client_log takes an iterable,
    # so a file handle keeps memory O(1) instead of materializing the whole file.
    if log_path is not None:
        try:
            with log_path.open(encoding="utf-8", errors="replace") as fh:
                client = parse_client_log(fh)
        except OSError:
            client = parse_client_log([])
    else:
        client = parse_client_log([])

    if output_json:
        doc = {
            "since": since,
            "client_log": str(log_path) if log_path else None,
            "routing": dataclasses.asdict(alloc),
            "saturation": dataclasses.asdict(sat),
            "time_weighted": dataclasses.asdict(weighted),
            "client": dataclasses.asdict(client),
        }
        if events is not None:
            doc["conditioned"] = {
                name: dataclasses.asdict(bucket)
                for name, bucket in conditioned_util(series, _compile_intervals(events)).items()
            }
        typer.echo(json.dumps(doc, indent=2, default=dict))
        return

    console.print(f"[bold]sccache-dist triage[/] (since {since})", highlight=False)

    console.print("[bold]scheduler routing (W1):[/]", highlight=False)
    console.print(f"  allocations: {alloc.total}", highlight=False)
    mis_style = "red" if alloc.misroute_pct >= 1 else "green"
    console.print(
        f"  misroutes: [{mis_style}]{alloc.misroutes} ({alloc.misroute_pct:.1f}%)[/] "
        f"(chose a busier server over a less-loaded one)",
        highlight=False,
    )
    console.print(f"  idle skips: {alloc.idle_skips} (skipped a zero-job server)", highlight=False)
    if alloc.truncated:
        console.print(
            f"  [yellow]truncated candidate lines: {alloc.truncated}[/] (excluded from the rate; a load==0 "
            f"break cut the candidate list - expect 0 once the scheduler tie-break fix is deployed)",
            highlight=False,
        )
    high_total = alloc.total_by_bucket.get("high", 0)
    if high_total:
        high_mis = alloc.misroutes_by_bucket.get("high", 0)
        hi_style = "red" if high_mis else "green"
        console.print(
            f"  high-load misroutes: [{hi_style}]{high_mis}/{high_total}[/] "
            f"(the actionable rate - a wrong choice while both nodes are loaded)",
            highlight=False,
        )
    if alloc.per_node_chosen:
        nodes = ", ".join(f"{addr} {n}" for addr, n in sorted(alloc.per_node_chosen.items()))
        console.print(f"  chosen per node: {nodes}", highlight=False)

    console.print("[bold]cluster saturation:[/]", highlight=False)
    console.print(
        f"  polls: {sat.samples}, ceiling: {sat.ceiling} cores, admission ceiling: {sat.admission_ceiling}",
        highlight=False,
    )
    console.print(f"  mean util: {sat.mean_util_pct:.1f}% ({sat.mean_inflight:.1f} in-flight)", highlight=False)
    if series:
        console.print(
            f"  time-weighted util: {weighted.mean_util_pct:.1f}% "
            f"(median cadence {weighted.median_cadence_s:.0f}s, max gap {weighted.max_gap_s:.0f}s - a large gap "
            f"means 'idle' may be 'unobserved')",
            highlight=False,
        )
    console.print(
        f"  idle: {sat.idle_pct:.1f}%  under-1/8: {sat.under_eighth_pct:.1f}%  "
        f"near-saturated: {sat.near_sat_pct:.1f}% (vs the admission ceiling)",
        highlight=False,
    )
    if events is not None and series:
        buckets = conditioned_util(series, _compile_intervals(events))
        console.print("  per-supply util (polls bucketed by live do_compile count):", highlight=False)
        for name, label in (("idle", "no compiles"), ("low", "1-7 compiles"), ("high", ">=8 compiles")):
            bucket = buckets[name]
            if bucket.polls:
                ratios = "  ".join(f"{addr} {r:.2f}" for addr, r in sorted(bucket.per_node_ratio.items()))
                console.print(
                    f"    {label}: {bucket.polls} polls, util {bucket.mean_util_pct:.1f}%   jobs/cores: {ratios}",
                    highlight=False,
                )
        console.print(
            "    [dim](high-supply bucket: a node far below its jobs/cores share = feed bottleneck)[/]",
            highlight=False,
        )

    console.print("[bold]client compiles (W2):[/]", highlight=False)
    console.print(f"  distributed jobs: {client.jobs}", highlight=False)
    if client.per_node_jobs:
        nodes = ", ".join(f"{addr} {n}" for addr, n in sorted(client.per_node_jobs.items()))
        console.print(f"  per node: {nodes}", highlight=False)
    if client.jobs:
        preproc = f", preprocess {client.mean_preprocess_ms:.0f}ms" if client.mean_preprocess_ms is not None else ""
        console.print(
            f"  mean per job: {client.mean_total_ms:.0f}ms "
            f"(put_tc {client.mean_put_tc_ms:.0f}ms, run+fetch {client.mean_run_fetch_ms:.0f}ms{preproc})",
            highlight=False,
        )
        if client.mean_preprocess_ms is None:
            console.print(
                "    [dim]preprocess timer absent - rebuild sccache with the W2 timer to measure it[/]",
                highlight=False,
            )
    if client.preproc_concurrency_max is not None:
        console.print(
            f"  preprocess concurrency: p95 {client.preproc_concurrency_p95}, max {client.preproc_concurrency_max} "
            f"(PC1 jobserver token pressure; pinned near the pool = the preprocessing wall)",
            highlight=False,
        )
    console.print(f"  local (conftest / not eligible): {client.not_eligible}", highlight=False)
    if client.fallback_reasons:
        console.print("  fallbacks to local:", highlight=False)
        for reason, n in client.fallback_reasons.most_common():
            console.print(f"    {n}x {reason}", highlight=False)

    console.print("[bold]rust distribution (W3):[/]", highlight=False)
    if client.rust_error_codes:
        codes = ", ".join(f"{code} x{n}" for code, n in client.rust_error_codes.most_common())
        console.print(f"  [red]remote rustc errors: {codes}[/]", highlight=False)
        console.print(
            "  rust compiles are failing on the remote (see the client log for the crate + error)",
            highlight=False,
        )
    else:
        console.print("  no remote rustc errors seen in the client log", highlight=False)
