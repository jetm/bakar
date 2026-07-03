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
from bakar.sched_triage import parse_client_log, parse_dist_alloc, parse_dist_status


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
            "client": dataclasses.asdict(client),
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
    if alloc.per_node_chosen:
        nodes = ", ".join(f"{addr} {n}" for addr, n in sorted(alloc.per_node_chosen.items()))
        console.print(f"  chosen per node: {nodes}", highlight=False)

    console.print("[bold]cluster saturation:[/]", highlight=False)
    console.print(f"  polls: {sat.samples}, ceiling: {sat.ceiling} cores", highlight=False)
    console.print(f"  mean util: {sat.mean_util_pct:.1f}% ({sat.mean_inflight:.1f} in-flight)", highlight=False)
    console.print(
        f"  idle: {sat.idle_pct:.1f}%  under-1/8: {sat.under_eighth_pct:.1f}%  near-saturated: {sat.near_sat_pct:.1f}%",
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
